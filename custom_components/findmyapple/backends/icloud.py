"""Shared FMIP pieces: the integration's error types and the device-record parser.

Devices come from the FMIP mobile service (see fmip.py), signed in as the account's emulated Mac.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..const import SOURCE_ICLOUD
from ..models import TrackedItem

# deviceStatus codes from fmipservice.
_STATUS_ONLINE = {"200"}
_CHARGING = {"Charging", "Charged"}


class AuthFailed(Exception):
    """Apple refused the credentials or the saved session; the user has to sign in again."""


class TwoFactorRequired(Exception):
    """The session needs a two-factor code before it can be used."""


class NotActivated(Exception):
    """Find My isn't available on the web for this account (e.g. Advanced Data Protection)."""


class Throttled(Exception):
    """Apple is throttling or temporarily unavailable. Back off and try later."""


class NoDevices(Exception):
    """The account has no Find My devices."""


@dataclass(slots=True)
class DeviceSummary:
    id: str
    name: str
    model: str
    owner: str | None


def parse_device(data: dict[str, Any], owner: str | None = None) -> TrackedItem:
    """Normalise one fmipservice device record. Pure function, unit-tested with fixtures."""
    location = data.get("location") or {}
    ts_ms = location.get("timeStamp")
    battery = data.get("batteryLevel")
    battery_status = data.get("batteryStatus")
    features = data.get("features") or {}
    lat = location.get("latitude")
    lon = location.get("longitude")
    return TrackedItem(
        id=data["id"],
        name=data.get("name") or data.get("deviceDisplayName") or data["id"],
        source=SOURCE_ICLOUD,
        model=data.get("deviceModel") or data.get("rawDeviceModel"),
        model_display_name=data.get("deviceDisplayName"),
        owner=owner,
        latitude=float(lat) if lat is not None else None,
        longitude=float(lon) if lon is not None else None,
        accuracy=location.get("horizontalAccuracy"),
        timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=UTC) if ts_ms else None,
        position_type=location.get("positionType"),
        is_old=bool(location.get("isOld", False)),
        is_inaccurate=bool(location.get("isInaccurate", False)),
        # batteryLevel is a 0..1 float; 0 with "Unknown" status means "not reported".
        battery_level=(
            round(float(battery) * 100)
            if battery is not None and not (battery == 0 and battery_status in (None, "Unknown"))
            else None
        ),
        battery_charging=(battery_status in _CHARGING) if battery_status not in (None, "Unknown") else None,
        low_power_mode=data.get("lowPowerMode"),
        online=str(data.get("deviceStatus")) in _STATUS_ONLINE if data.get("deviceStatus") is not None else None,
        can_play_sound=bool(features.get("SND", False)),
        raw=data,
    )
