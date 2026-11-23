"""Apple "Find My iPhone" (FMIP) devices, using the emulated-Mac session of the account.

The same sign-in that unlocks the Find My network tags also yields an FMIP app token
(`mmeFMIPAppToken`). With the Mac's anisette headers it works against the FMIP mobile service, so
one login covers iPhones, iPads, Macs, family devices *and* tags. No separate web login.

Apple signs `fmipmobile.icloud.com` with its own PKI (Apple Server Authentication CA → Apple Root
CA), which is not in the usual public CA bundles, so the root is bundled and pinned for this host.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import ssl
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..models import TrackedItem
from .icloud import AuthFailed, NoDevices, Throttled, parse_device

if TYPE_CHECKING:
    from findmy import AsyncAppleAccount

_LOGGER = logging.getLogger(__name__)

APPLE_ROOT_CA = Path(__file__).resolve().parent.parent / "apple_root_ca.pem"
DEFAULT_HOST = "p01-fmipmobile.icloud.com"
TOKEN_NAME = "mmeFMIPAppToken"
_USER_AGENT = "FindMyiPhone/500 CFNetwork/758.0.2 Darwin/15.0.0"
_TIMEOUT = 30.0
# FMIP answers "use another partition host" with this status and an X-Apple-MMe-Host header.
_REDIRECT = 330


def _client_context(shouldLocate: bool) -> dict[str, Any]:
    return {
        "clientContext": {
            "fmly": True,
            "shouldLocate": shouldLocate,
            "selectedDevice": "all",
            "deviceListVersion": 1,
            "appName": "FindMyiPhone",
            "appVersion": "6.0",
        }
    }


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(APPLE_ROOT_CA))


class FmipClient:
    """Lists and locates an account's FMIP devices with an already signed-in `AsyncAppleAccount`."""

    def __init__(self, account: AsyncAppleAccount) -> None:
        self._account = account
        self._host = DEFAULT_HOST
        self._http: httpx.AsyncClient | None = None
        self._renew_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            context = await asyncio.to_thread(_ssl_context)  # reads the CA file
            self._http = httpx.AsyncClient(timeout=_TIMEOUT, verify=context, follow_redirects=False)
        return self._http

    def _url(self, action: str) -> str:
        return f"https://{self._host}/fmipservice/device/{self._account.dsid}/{action}"

    async def _headers(self) -> dict[str, str]:
        token = (self._account.service_tokens or {}).get(TOKEN_NAME)
        if not token:
            raise AuthFailed(f"The sign-in has no {TOKEN_NAME}")
        auth = base64.b64encode(f"{self._account.dsid}:{token}".encode()).decode()
        return {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json; charset=utf-8",
            "X-Apple-Realm-Support": "1.0",
            "X-Apple-Find-API-Ver": "3.0",
            "X-Apple-AuthScheme": "Forever",
            "User-Agent": _USER_AGENT,
            # Apple only accepts the token together with the headers of the Mac that signed in.
            **await self._account.get_anisette_headers(),
        }

    async def _post(self, action: str, body: dict[str, Any]) -> httpx.Response:
        http = await self._client()
        try:
            resp = await http.post(self._url(action), headers=await self._headers(), json=body)
            if resp.status_code == _REDIRECT and (host := resp.headers.get("X-Apple-MMe-Host")):
                self._host = host
                resp = await http.post(self._url(action), headers=await self._headers(), json=body)
        except httpx.HTTPError as err:
            raise Throttled(f"{type(err).__name__}: {err}") from err
        return resp

    async def _request(self, action: str, body: dict[str, Any]) -> httpx.Response:
        resp = await self._post(action, body)
        if resp.status_code == 401:
            await self._renew()
            resp = await self._post(action, body)
        if resp.status_code in (401, 403):
            raise AuthFailed(f"FMIP refused the session (HTTP {resp.status_code})")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise Throttled(f"FMIP unavailable (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise Throttled(f"FMIP answered HTTP {resp.status_code}")
        return resp

    async def _renew(self) -> None:
        """The token expired: sign in again with the stored password (a Grand Slam call from the Mac)."""
        from findmy import LoginState

        async with self._renew_lock:
            account = self._account
            if not getattr(account, "_username", None) or not getattr(account, "_password", None):
                raise AuthFailed("FMIP token expired and no password is stored to renew it")
            _LOGGER.debug("FMIP token expired, renewing the session")
            try:
                if await account._gsa_authenticate() != LoginState.AUTHENTICATED:
                    raise AuthFailed("Renewing the Find My sign-in needs a new two-factor code")
                await account._login_mobileme()
            except AuthFailed:
                raise
            except Exception as err:
                raise Throttled(f"Could not renew the sign-in: {type(err).__name__}: {err}") from err

    async def fetch(self, *, locate: bool) -> dict[str, TrackedItem]:
        """All devices of the account (and family). `locate=True` asks each for a fresh GPS fix."""
        resp = await self._request("initClient", _client_context(locate))
        try:
            payload = resp.json()
        except ValueError as err:
            raise Throttled("FMIP returned a page that is not JSON") from err
        return parse_devices(payload)

    async def play_sound(self, device_id: str) -> None:
        body = {"device": device_id, "subject": "Home Assistant"}
        await self._request("playSound", body)


def parse_devices(payload: dict[str, Any]) -> dict[str, TrackedItem]:
    """Normalise an `initClient` response. Pure function, unit-tested with fixtures."""
    devices = payload.get("content") or []
    if not devices:
        raise NoDevices("The account has no Find My devices")
    members = {
        prs_id: info.get("firstName") or prs_id
        for prs_id, info in ((payload.get("userInfo") or {}).get("membersInfo") or {}).items()
    }
    items = {}
    for data in devices:
        prs_id = data.get("prsId")
        item = parse_device(data, members.get(prs_id) if prs_id else None)
        items[item.id] = item
    return items
