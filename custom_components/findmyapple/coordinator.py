"""Coordinators: iCloud devices (adaptive polling) and Find My network tags. Shared: backoff, restore."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.location import distance

from .backends.icloud import (
    AuthFailed,
    ICloudBackend,
    NoDevices,
    NotActivated,
    Throttled,
    TwoFactorRequired,
)
from .const import (
    AWAY_MAX_DISTANCE_KM,
    BACKOFF_MAX,
    BACKOFF_START,
    CONF_AWAY_MAX_INTERVAL,
    CONF_AWAY_MIN_INTERVAL,
    CONF_HOME_INTERVAL,
    CONF_TAG_INTERVAL,
    CONF_TRACKED_DEVICES,
    CONF_TRACKED_TAGS,
    DEFAULT_AWAY_MAX_INTERVAL,
    DEFAULT_AWAY_MIN_INTERVAL,
    DEFAULT_HOME_INTERVAL,
    DEFAULT_TAG_INTERVAL,
    DOMAIN,
    STORAGE_VERSION,
)
from .models import TrackedItem

if TYPE_CHECKING:
    from . import FindMyAppleConfigEntry
    from .backends.findmy_network import TagSession

_LOGGER = logging.getLogger(__name__)


def locations_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.locations")


class FindMyAppleCoordinator(DataUpdateCoordinator[dict[str, TrackedItem]]):
    """Shared behaviour: tracked-item filter, last-good-location merge, backoff, restore on restart."""

    config_entry: FindMyAppleConfigEntry
    tracked_option: str = CONF_TRACKED_DEVICES

    def __init__(self, hass: HomeAssistant, entry: FindMyAppleConfigEntry, name: str, interval: timedelta) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {name} {entry.title}", update_interval=interval
        )
        self._store = locations_store(hass, entry.entry_id)
        self._backoff: timedelta | None = None
        self.all_devices: dict[str, str] = {}  # every item on the account (id -> name), tracked or not

    async def async_restore(self) -> None:
        """Load the last known locations so entities have a position right after a restart."""
        stored = await self._store.async_load() or {}
        items = {}
        for raw in stored.get("items", []):
            try:
                item = TrackedItem.from_storage(raw)
            except (KeyError, ValueError):
                continue
            if self._is_tracked(item.id):
                items[item.id] = item
        if items:
            self.async_set_updated_data(items)

    def _is_tracked(self, item_id: str) -> bool:
        tracked = self.config_entry.options.get(self.tracked_option)
        return tracked is None or item_id in tracked

    def _set_backoff(self) -> None:
        self._backoff = BACKOFF_START if self._backoff is None else min(self._backoff * 2, BACKOFF_MAX)
        self.update_interval = self._backoff

    async def _merge_and_save(self, fresh: dict[str, TrackedItem]) -> dict[str, TrackedItem]:
        self.all_devices = {item_id: item.name for item_id, item in fresh.items()}
        previous = self.data or {}
        result = {
            item_id: item.merge_previous(previous.get(item_id))
            for item_id, item in fresh.items()
            if self._is_tracked(item_id)
        }
        self._backoff = None
        await self._store.async_save({"items": [item.to_storage() for item in result.values()]})
        return result

    async def async_locate_now(self) -> None:
        await self.async_request_refresh()

    async def async_play_sound(self, item_id: str) -> None:
        raise HomeAssistantError("This item can't play a sound")


class ICloudCoordinator(FindMyAppleCoordinator):
    """Polls one Apple account's iCloud devices, with an interval that adapts to distance from zones."""

    def __init__(self, hass: HomeAssistant, entry: FindMyAppleConfigEntry, backend: ICloudBackend) -> None:
        super().__init__(
            hass, entry, "iCloud", timedelta(minutes=entry.options.get(CONF_HOME_INTERVAL, DEFAULT_HOME_INTERVAL))
        )
        self.backend = backend
        self._logged_in = False

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.hass.async_add_executor_job(self.backend.close)

    async def _async_update_data(self) -> dict[str, TrackedItem]:
        try:
            if not self._logged_in:
                await self.hass.async_add_executor_job(self.backend.login)
                self._logged_in = True
            fresh = await self.hass.async_add_executor_job(self.backend.fetch, True)
        except (AuthFailed, TwoFactorRequired) as err:
            self._logged_in = False
            raise ConfigEntryAuthFailed(str(err)) from err
        except NotActivated as err:
            self._set_backoff()
            raise UpdateFailed(
                "Find My is not available on iCloud.com for this account. With Advanced Data Protection, "
                "turn on 'Access iCloud Data on the Web' on your iPhone."
            ) from err
        except (Throttled, NoDevices) as err:
            self._set_backoff()
            raise UpdateFailed(f"iCloud temporarily unavailable: {err}") from err

        result = await self._merge_and_save(fresh)
        self.update_interval = self._next_interval(result)
        return result

    def _next_interval(self, items: dict[str, TrackedItem]) -> timedelta:
        """Shortest interval any tracked device needs: home → home interval; away → scaled by distance."""
        options = self.config_entry.options
        home = timedelta(minutes=options.get(CONF_HOME_INTERVAL, DEFAULT_HOME_INTERVAL))
        away_min = timedelta(minutes=options.get(CONF_AWAY_MIN_INTERVAL, DEFAULT_AWAY_MIN_INTERVAL))
        away_max = timedelta(minutes=options.get(CONF_AWAY_MAX_INTERVAL, DEFAULT_AWAY_MAX_INTERVAL))
        zones = self._zones()
        intervals = [
            interval_for_distance(nearest_zone_km(item, zones), home, away_min, away_max)
            for item in items.values()
            if item.has_location
        ]
        return min(intervals, default=home)

    def _zones(self) -> list[tuple[float, float, float]]:
        zones = []
        for state in self.hass.states.async_all("zone"):
            lat, lon = state.attributes.get("latitude"), state.attributes.get("longitude")
            if lat is not None and lon is not None and not state.attributes.get("passive", False):
                zones.append((lat, lon, float(state.attributes.get("radius", 0))))
        if not zones:  # zone.home always exists in a running HA; fall back to the configured home
            zones.append((self.hass.config.latitude, self.hass.config.longitude, 100.0))
        return zones

    async def async_play_sound(self, item_id: str) -> None:
        try:
            await self.hass.async_add_executor_job(self.backend.play_sound, item_id)
        except (AuthFailed, TwoFactorRequired) as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError("Sign in to iCloud again to play a sound") from err
        except (Throttled, NotActivated, NoDevices) as err:
            raise HomeAssistantError(f"Could not play sound: {err}") from err


class TagCoordinator(FindMyAppleCoordinator):
    """Polls an account's Find My network tags with one batched report fetch per interval."""

    tracked_option = CONF_TRACKED_TAGS

    def __init__(
        self,
        hass: HomeAssistant,
        entry: FindMyAppleConfigEntry,
        session: TagSession,
        state_store: Store[dict[str, Any]],
    ) -> None:
        super().__init__(
            hass, entry, "tags", timedelta(minutes=entry.options.get(CONF_TAG_INTERVAL, DEFAULT_TAG_INTERVAL))
        )
        self.session = session
        self._state_store = state_store
        self._interval = self.update_interval

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.session.close()

    async def _async_update_data(self) -> dict[str, TrackedItem]:
        from .backends.findmy_network import MachineError

        tracked = self.config_entry.options.get(CONF_TRACKED_TAGS, list(self.session.accessories))
        try:
            fresh = await self.session.fetch(tracked)
        except (AuthFailed, TwoFactorRequired) as err:
            await self._save_state()  # keep whatever the library refreshed before failing
            raise ConfigEntryAuthFailed(str(err)) from err
        except MachineError as err:
            self._set_backoff()
            raise UpdateFailed(f"Apple refused the emulated Mac's headers: {err}") from err
        except Throttled as err:
            self._set_backoff()
            raise UpdateFailed(f"Find My network temporarily unavailable: {err}") from err

        # Key alignments (and possibly refreshed tokens) changed: persist them every poll.
        await self._save_state()
        result = await self._merge_and_save(fresh)
        self.all_devices = {i: a.name or i for i, a in self.session.accessories.items()}
        self.update_interval = self._interval
        return result

    async def _save_state(self) -> None:
        await self._state_store.async_save(self.session.to_state())


def nearest_zone_km(item: TrackedItem, zones: list[tuple[float, float, float]]) -> float:
    """Distance in km from the item to the edge of the nearest zone (0 when inside one)."""
    assert item.latitude is not None and item.longitude is not None
    best = float("inf")
    for lat, lon, radius in zones:
        metres = distance(item.latitude, item.longitude, lat, lon)
        if metres is not None:
            best = min(best, max(0.0, metres - radius) / 1000)
    return 0.0 if best == float("inf") else best


def interval_for_distance(
    km: float, home: timedelta, away_min: timedelta, away_max: timedelta
) -> timedelta:
    """Inside a zone → `home`. Outside: `away_min` right at the edge, rising linearly to `away_max`."""
    if km <= 0:
        return home
    fraction = min(km / AWAY_MAX_DISTANCE_KM, 1.0)
    return away_min + (away_max - away_min) * fraction
