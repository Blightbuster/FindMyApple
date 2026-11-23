"""Coordinators: iCloud devices (adaptive polling) and Find My network tags. Shared: backoff, restore."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.location import distance

from .backends.icloud import (
    AuthFailed,
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
    CONF_INCLUDE_FAMILY,
    CONF_TAG_INTERVAL,
    CONF_TRACKED_ITEMS,
    DEFAULT_AWAY_MAX_INTERVAL,
    DEFAULT_AWAY_MIN_INTERVAL,
    DEFAULT_HOME_INTERVAL,
    DEFAULT_TAG_INTERVAL,
    DOMAIN,
    SOURCE_ICLOUD,
    STORAGE_VERSION,
)
from .models import TrackedItem

if TYPE_CHECKING:
    from . import FindMyAppleConfigEntry
    from .backends.findmy_network import TagSession
    from .backends.fmip import FmipClient

_LOGGER = logging.getLogger(__name__)


def locations_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.locations")


class FindMyAppleCoordinator(DataUpdateCoordinator[dict[str, TrackedItem]]):
    """Shared behaviour: tracked-item filter, last-good-location merge, backoff, restore on restart."""

    config_entry: FindMyAppleConfigEntry
    tracked_option: str = CONF_TRACKED_ITEMS

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


class AccountCoordinator(FindMyAppleCoordinator):
    """One Apple account, one sign-in: FMIP devices (adaptive polling) plus Find My network tags.

    Both come from the same emulated-Mac session. Devices are polled with an interval that adapts to
    the distance from zones; tags (whose keys rotate every 15 minutes) at most once per tag interval.
    """

    tracked_option = CONF_TRACKED_ITEMS

    def __init__(
        self,
        hass: HomeAssistant,
        entry: FindMyAppleConfigEntry,
        session: TagSession,
        fmip: FmipClient,
        state_store: Store[dict[str, Any]],
    ) -> None:
        super().__init__(
            hass, entry, "account", timedelta(minutes=entry.options.get(CONF_HOME_INTERVAL, DEFAULT_HOME_INTERVAL))
        )
        self.session = session
        self.fmip = fmip
        self._state_store = state_store
        self._last_tags: datetime | None = None

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.fmip.close()
        await self.session.close()

    @property
    def _tag_interval(self) -> timedelta:
        return timedelta(minutes=self.config_entry.options.get(CONF_TAG_INTERVAL, DEFAULT_TAG_INTERVAL))

    def _tag_ids(self) -> list[str]:
        """The account's tags. Its own Apple devices are in the keychain too, but FMIP covers them better."""
        from .backends.findmy_network import describe

        return [i for i, a in self.session.accessories.items() if not describe(a).is_apple_device]

    async def _async_update_data(self) -> dict[str, TrackedItem]:
        from .backends.findmy_network import MachineError

        previous = self.data or {}
        fresh: dict[str, TrackedItem] = {}
        errors: list[Exception] = []

        try:
            devices = await self.fmip.fetch(locate=True)
            if not self.config_entry.options.get(CONF_INCLUDE_FAMILY, True):
                devices = {i: d for i, d in devices.items() if d.owner is None}
            fresh.update(devices)
        except (NotActivated, NoDevices) as err:
            _LOGGER.debug("No FMIP devices: %s", err)
        except (AuthFailed, TwoFactorRequired, Throttled, MachineError) as err:
            errors.append(err)
            fresh.update({i: it for i, it in previous.items() if it.source == SOURCE_ICLOUD})

        now = datetime.now(UTC)
        tags_due = self._last_tags is None or now - self._last_tags >= self._tag_interval
        tag_ids = self._tag_ids()
        tracked = self.config_entry.options.get(CONF_TRACKED_ITEMS)
        wanted = tag_ids if tracked is None else [i for i in tag_ids if i in tracked]
        if wanted and tags_due:
            try:
                fresh.update(await self.session.fetch(wanted))
                self._last_tags = now
            except (AuthFailed, TwoFactorRequired, Throttled, MachineError) as err:
                errors.append(err)
                fresh.update({i: it for i, it in previous.items() if it.source != SOURCE_ICLOUD})
            # Key alignments (and possibly refreshed tokens) changed: persist them.
            await self._save_state()
        else:
            fresh.update({i: it for i, it in previous.items() if it.source != SOURCE_ICLOUD})
        # Tags not fetched this round still need to be listed, so the options can offer them.
        tag_names = {i: self.session.accessories[i].name or i for i in tag_ids}

        if errors and not fresh:
            err = errors[0]
            if isinstance(err, (AuthFailed, TwoFactorRequired)):
                raise ConfigEntryAuthFailed(str(err)) from err
            self._set_backoff()
            raise UpdateFailed(f"Apple temporarily unavailable: {err}") from err
        if errors:
            auth = [e for e in errors if isinstance(e, (AuthFailed, TwoFactorRequired))]
            if auth:
                raise ConfigEntryAuthFailed(str(auth[0])) from auth[0]
            _LOGGER.warning("Part of the update failed, keeping the last known data: %s", errors[0])
        backoff = self._backoff
        result = await self._merge_and_save(fresh)  # resets the backoff: a degraded update is not a success
        self.all_devices.update(tag_names)
        if errors:
            self._backoff = backoff
            self._set_backoff()
        else:
            self.update_interval = self._next_interval(result)
        return result

    async def _save_state(self) -> None:
        await self._state_store.async_save(self.session.to_state())

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
            if item.has_location and item.source == SOURCE_ICLOUD
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
            await self.fmip.play_sound(item_id)
        except (AuthFailed, TwoFactorRequired) as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError("Sign in to Apple again to play a sound") from err
        except (Throttled, NotActivated, NoDevices) as err:
            raise HomeAssistantError(f"Could not play sound: {err}") from err


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
