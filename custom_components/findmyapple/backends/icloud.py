"""iCloud "Find My iPhone" backend (the iCloud web API, as icloud.com/find and iCloud3 use).

pyicloud is synchronous. Every public method here blocks and must run in an executor
(`hass.async_add_executor_job`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloudAPIResponseException,
    PyiCloudAuthRequiredException,
    PyiCloudException,
    PyiCloudFailedLoginException,
    PyiCloudNoDevicesException,
    PyiCloudNoStoredPasswordAvailableException,
    PyiCloudServiceNotActivatedException,
    PyiCloudServiceUnavailable,
)

from ..const import SOURCE_ICLOUD
from ..models import TrackedItem

_LOGGER = logging.getLogger(__name__)

# pyicloud starts its own refresh thread. We poll from the coordinator instead, so push that
# thread's interval out of the way (it only ever waits on an Event, so a long timeout is cheap).
_PYICLOUD_THREAD_INTERVAL = 365 * 24 * 3600.0

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


class ICloudBackend:
    """One Apple account on the iCloud web API."""

    def __init__(
        self,
        apple_id: str,
        password: str | None,
        session_dir: Path,
        *,
        china: bool = False,
        with_family: bool = True,
    ) -> None:
        self.apple_id = apple_id
        self._password = password
        self._session_dir = session_dir
        self._china = china
        self._with_family = with_family
        self._api: PyiCloudService | None = None
        self._members: dict[str, str] = {}  # prsId -> first name

    # -- login ------------------------------------------------------------------------------------

    def login(self) -> None:
        """Sign in, reusing the saved session/trust token. Raises AuthFailed / TwoFactorRequired."""
        self._session_dir.mkdir(parents=True, exist_ok=True)
        try:
            api = PyiCloudService(
                self.apple_id,
                self._password,
                cookie_directory=str(self._session_dir),
                china_mainland=self._china,
                with_family=self._with_family,
                refresh_interval=_PYICLOUD_THREAD_INTERVAL,
                authenticate=False,  # no keyring lookup; we call authenticate() ourselves
            )
            api.authenticate()
        except PyiCloudNoStoredPasswordAvailableException as err:
            raise AuthFailed(str(err)) from err
        except PyiCloudFailedLoginException as err:
            raise _login_error(err) from err
        except PyiCloudServiceUnavailable as err:
            raise Throttled(str(err)) from err
        except PyiCloudAPIResponseException as err:
            raise _classify(err) from err
        self._api = api
        if api.requires_2fa or api.requires_2sa:
            raise TwoFactorRequired

    @property
    def requires_2fa(self) -> bool:
        return self._api is not None and (self._api.requires_2fa or self._api.requires_2sa)

    @property
    def uses_security_key(self) -> bool:
        return bool(self._api is not None and self._api.security_key_names)

    @property
    def two_factor_delivery(self) -> str:
        """How the pending code was sent ("trusted_device", "sms", "security_key", ...)."""
        return self._api.two_factor_delivery_method if self._api is not None else "unknown"

    def request_2fa_code(self) -> str:
        """Ask Apple to send a (new) code. login() already sends one. Returns the delivery method."""
        api = self._require_api()
        if api.security_key_names:
            return "security_key"
        api.request_2fa_code()
        return api.two_factor_delivery_method

    def validate_2fa_code(self, code: str) -> bool:
        api = self._require_api()
        try:
            ok = api.validate_2fa_code(code)
        except PyiCloudAPIResponseException:
            return False
        if ok and not api.is_trusted_session:
            api.trust_session()
        return ok and not self.requires_2fa

    # -- Find My ----------------------------------------------------------------------------------

    def list_devices(self) -> list[DeviceSummary]:
        """All Find My devices of the account (and family, if enabled), without forcing a locate."""
        devices = self._devices(locate=False)
        return [
            DeviceSummary(
                id=d.data["id"],
                name=d.data.get("name") or d.data.get("deviceDisplayName") or d.data["id"],
                model=d.data.get("deviceDisplayName") or d.data.get("deviceModel") or "",
                owner=self._owner(d.data),
            )
            for d in devices
        ]

    def fetch(self, locate: bool) -> dict[str, TrackedItem]:
        """Refresh all devices. `locate=True` asks every device for a fresh GPS fix."""
        return {d.data["id"]: self._to_item(d.data) for d in self._devices(locate=locate)}

    def play_sound(self, device_id: str) -> None:
        manager = self._manager()
        try:
            manager.devices[device_id].play_sound("Home Assistant")
        except KeyError as err:
            raise NoDevices(f"Unknown device {device_id}") from err
        except PyiCloudAPIResponseException as err:
            raise _classify(err) from err

    def close(self) -> None:
        if self._api is not None and self._api._devices is not None:
            self._api._devices.stop_event.set()

    # -- internals --------------------------------------------------------------------------------

    def _require_api(self) -> PyiCloudService:
        if self._api is None:
            self.login()
        assert self._api is not None
        return self._api

    def _manager(self) -> Any:
        api = self._require_api()
        if api.requires_2fa or api.requires_2sa:
            raise TwoFactorRequired
        try:
            return api.devices
        except PyiCloudNoDevicesException as err:
            raise NoDevices from err
        except PyiCloudServiceNotActivatedException as err:
            raise NotActivated(str(err)) from err
        except (PyiCloudAuthRequiredException, PyiCloud2FARequiredException) as err:
            raise AuthFailed(str(err)) from err
        except PyiCloudFailedLoginException as err:
            raise _login_error(err) from err
        except PyiCloudServiceUnavailable as err:
            raise Throttled(str(err)) from err
        except PyiCloudAPIResponseException as err:
            raise _classify(err) from err

    def _devices(self, locate: bool) -> list[Any]:
        manager = self._manager()
        try:
            manager.refresh(locate=locate)
        except PyiCloudNoDevicesException as err:
            raise NoDevices from err
        except (PyiCloudAuthRequiredException, PyiCloud2FARequiredException) as err:
            raise AuthFailed(str(err)) from err
        except PyiCloudFailedLoginException as err:
            raise _login_error(err) from err
        except PyiCloudServiceUnavailable as err:
            raise Throttled(str(err)) from err
        except PyiCloudAPIResponseException as err:
            raise _classify(err) from err
        except PyiCloudException as err:
            raise Throttled(str(err)) from err
        finally:
            manager.stop_event.set()  # refresh() restarts pyicloud's thread; keep it stopped
        user_info = manager.user_info or {}
        self._members = {
            prs_id: info.get("firstName") or prs_id
            for prs_id, info in (user_info.get("membersInfo") or {}).items()
        }
        return list(manager)

    def _owner(self, data: dict[str, Any]) -> str | None:
        prs_id = data.get("prsId")
        return self._members.get(prs_id) if prs_id else None

    def _to_item(self, data: dict[str, Any]) -> TrackedItem:
        return parse_device(data, self._owner(data))


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


_CREDENTIAL_ERRORS = ("Invalid email/password", "No password set", "No session token")


def _login_error(err: PyiCloudFailedLoginException) -> Exception:
    """A refused password means "sign in again"; anything else (network, 5xx) is transient."""
    text = str(err)
    if any(marker in text for marker in _CREDENTIAL_ERRORS):
        return AuthFailed(text)
    return Throttled(text)


def _classify(err: PyiCloudAPIResponseException) -> Exception:
    code = getattr(err, "code", None)
    text = str(err)
    if code in (401, 403, 421, 450) or "Missing X-APPLE-WEBAUTH-TOKEN" in text:
        return AuthFailed(text)
    if code == 503 or code == 429 or (isinstance(code, int) and 500 <= code < 600) or "-900007" in text:
        return Throttled(text)
    if "ZONE_NOT_FOUND" in text or "Access iCloud Data on the Web" in text or "ACCESS_DENIED" in text:
        return NotActivated(text)
    return Throttled(text)  # unknown API errors: back off rather than force a sign-in


def now_utc() -> datetime:
    return datetime.now(UTC)
