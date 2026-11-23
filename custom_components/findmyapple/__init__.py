"""FindMyApple: track Apple devices and Find My tags in Home Assistant.

Each config entry is one Apple account's iCloud devices (`kind: icloud`) or its Find My network
tags (`kind: tags`). They sign in separately, so one failing never breaks the other.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .backends.icloud import ICloudBackend
from .const import CONF_INCLUDE_FAMILY, CONF_KIND, CONF_REGION, DOMAIN, KIND_TAGS, REGION_CHINA, STORAGE_VERSION
from .coordinator import FindMyAppleCoordinator, ICloudCoordinator, TagCoordinator, locations_store

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


def session_dir(hass: HomeAssistant, unique_id: str) -> Path:
    """Where pyicloud keeps this account's session and trust token (inside .storage)."""
    return Path(hass.config.path(".storage", DOMAIN, unique_id))


def machine_root(hass: HomeAssistant, unique_id: str) -> Path:
    """The account's emulated Mac. Kept even when the entry is removed: re-adding reuses it, so no
    second "MacBook Pro" appears in the user's device list (Apple provisions a device id once)."""
    return Path(hass.config.path(".storage", DOMAIN, f"mac-{_safe(unique_id)}"))


def tag_state_store(hass: HomeAssistant, unique_id: str) -> Store[dict[str, Any]]:
    """FindMy.py session (incl. password and tokens) and the tags' keys and alignments."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.tags.{_safe(unique_id)}", private=True)


def make_backend(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> ICloudBackend:
    return ICloudBackend(
        entry.data[CONF_USERNAME],
        entry.data.get(CONF_PASSWORD),
        session_dir(hass, entry.unique_id or entry.entry_id),
        china=entry.data.get(CONF_REGION) == REGION_CHINA,
        with_family=entry.options.get(CONF_INCLUDE_FAMILY, True),
    )


async def _make_tag_coordinator(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> TagCoordinator:
    from .backends.findmy_network import MachineError, TagSession, async_open_machine

    assert entry.unique_id is not None
    store = tag_state_store(hass, entry.unique_id)
    state = await store.async_load()
    if not state or "account" not in state:
        raise ConfigEntryAuthFailed("No saved Find My session; sign in again")
    try:
        # Never provision here: a restored session only works on the machine that signed it in.
        provider = await async_open_machine(machine_root(hass, entry.unique_id), provision=False)
    except MachineError as err:
        raise ConfigEntryNotReady(f"Emulated Mac unavailable: {err}") from err
    session = TagSession.from_state(provider, state)
    return TagCoordinator(hass, entry, session, store)


async def async_setup_entry(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> bool:
    coordinator: FindMyAppleCoordinator
    if entry.data.get(CONF_KIND) == KIND_TAGS:
        coordinator = await _make_tag_coordinator(hass, entry)
    else:
        coordinator = ICloudCoordinator(hass, entry, make_backend(hass, entry))
    await coordinator.async_restore()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = RuntimeData(coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> None:
    """Delete saved sessions, tag keys and stored locations of a removed entry (not the emulated Mac)."""
    await locations_store(hass, entry.entry_id).async_remove()
    unique_id = entry.unique_id or entry.entry_id
    if entry.data.get(CONF_KIND) == KIND_TAGS:
        await tag_state_store(hass, unique_id).async_remove()
    else:
        await hass.async_add_executor_job(partial(shutil.rmtree, session_dir(hass, unique_id), ignore_errors=True))


async def _async_options_updated(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
