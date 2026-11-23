"""FindMyApple: track Apple devices and Find My tags in Home Assistant.

Each config entry is one Apple account. One sign-in, as the account's emulated Mac, provides both
the Find My iPhone devices (iPhone, iPad, Mac, family) and the Find My network tags.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION
from .coordinator import AccountCoordinator, FindMyAppleCoordinator, locations_store

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
]


@dataclass
class RuntimeData:
    coordinator: FindMyAppleCoordinator


type FindMyAppleConfigEntry = ConfigEntry[RuntimeData]


def _safe(unique_id: str) -> str:
    return hashlib.sha256(unique_id.encode()).hexdigest()[:16]


def machine_root(hass: HomeAssistant, unique_id: str) -> Path:
    """The account's emulated Mac. Kept even when the entry is removed: re-adding reuses it, so no
    second "MacBook Pro" appears in the user's device list (Apple provisions a device id once)."""
    return Path(hass.config.path(".storage", DOMAIN, f"mac-{_safe(unique_id)}"))


def account_state_store(hass: HomeAssistant, unique_id: str) -> Store[dict[str, Any]]:
    """The signed-in session (incl. password and tokens) and the tags' keys and alignments."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.account.{_safe(unique_id)}", private=True)


async def _make_coordinator(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> AccountCoordinator:
    from .backends.findmy_network import MachineError, TagSession, async_open_machine
    from .backends.fmip import FmipClient

    assert entry.unique_id is not None
    store = account_state_store(hass, entry.unique_id)
    state = await store.async_load()
    if not state or "account" not in state:
        raise ConfigEntryAuthFailed("No saved Apple sign-in; sign in again")
    try:
        # Never provision here: a restored session only works on the machine that signed it in.
        provider = await async_open_machine(machine_root(hass, entry.unique_id), provision=False)
    except MachineError as err:
        raise ConfigEntryNotReady(f"Emulated Mac unavailable: {err}") from err
    session = TagSession.from_state(provider, state)
    return AccountCoordinator(hass, entry, session, FmipClient(session.account), store)


async def async_setup_entry(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> bool:
    coordinator = await _make_coordinator(hass, entry)
    await coordinator.async_restore()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = RuntimeData(coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> None:
    """Delete the saved session, tag keys and stored locations of a removed entry (not the emulated Mac)."""
    await locations_store(hass, entry.entry_id).async_remove()
    await account_state_store(hass, entry.unique_id or entry.entry_id).async_remove()


async def _async_options_updated(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
