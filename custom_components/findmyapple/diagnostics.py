"""Diagnostics: a redacted dump of the entry and the latest device data."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import FindMyAppleConfigEntry

TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    "title",
    "unique_id",
    "latitude",
    "longitude",
    "id",
    "prsId",
    "baUUID",
    "name",
    "firstName",
    "lastName",
    "appleId",
    "email",
    "phoneNumber",
    "address",
    "lostDevice",
    "msg",
    "snd",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: FindMyAppleConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data.coordinator
    devices = [
        {
            "source": item.source,
            "model": item.model,
            "has_location": item.has_location,
            "accuracy": item.accuracy,
            "timestamp": item.timestamp.isoformat() if item.timestamp else None,
            "position_type": item.position_type,
            "is_old": item.is_old,
            "battery_level": item.battery_level,
            "online": item.online,
            "raw": async_redact_data(item.raw, TO_REDACT),
        }
        for item in (coordinator.data or {}).values()
    ]
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "update_interval_s": coordinator.update_interval.total_seconds() if coordinator.update_interval else None,
        "last_update_success": coordinator.last_update_success,
        "known_device_count": len(coordinator.all_devices),
        "devices": devices,
    }
