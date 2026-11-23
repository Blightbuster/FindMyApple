"""FMIP client: auth headers, partition redirect, token renewal, error mapping, record parsing."""

from __future__ import annotations

import base64

import httpx
import pytest
from findmy import LoginState

from custom_components.findmyapple.backends import fmip
from custom_components.findmyapple.backends.icloud import AuthFailed, NoDevices, Throttled

from .conftest import TOKENS, load_devices


class FakeApple:
    """The bits of AsyncAppleAccount the client touches."""

    dsid = "1234567890"

    def __init__(self, *, password: str | None = "pw") -> None:
        self.service_tokens = dict(TOKENS)
        self._username = "alex@example.com"
        self._password = password
        self.renewals = 0

    async def get_anisette_headers(self) -> dict[str, str]:
        return {"X-Apple-I-MD": "otp", "X-Apple-I-MD-M": "machine"}

    async def _gsa_authenticate(self) -> LoginState:
        self.renewals += 1
        self.service_tokens["mmeFMIPAppToken"] = "renewed"
        return LoginState.AUTHENTICATED

    async def _login_mobileme(self) -> None:
        pass


def _client(account: FakeApple, handler) -> fmip.FmipClient:
    client = fmip.FmipClient(account)  # type: ignore[arg-type]
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"content": load_devices()})


async def test_sends_the_app_token_with_the_macs_anisette_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(request)

    items = await _client(FakeApple(), handler).fetch(locate=True)
    assert "iphone-1" in items
    request = seen[0]
    assert request.url.path == "/fmipservice/device/1234567890/initClient"
    assert request.headers["authorization"] == "Basic " + base64.b64encode(b"1234567890:f").decode()
    assert request.headers["x-apple-i-md"] == "otp"
    assert b'"shouldLocate":true' in request.content.replace(b" ", b"")


async def test_listing_does_not_ask_devices_to_locate() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return _ok(request)

    await _client(FakeApple(), handler).fetch(locate=False)
    assert b'"shouldLocate":false' in bodies[0].replace(b" ", b"")


async def test_follows_the_partition_host_redirect() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == fmip.DEFAULT_HOST:
            return httpx.Response(330, headers={"X-Apple-MMe-Host": "p42-fmipmobile.icloud.com"})
        return _ok(request)

    client = _client(FakeApple(), handler)
    await client.fetch(locate=True)
    await client.fetch(locate=True)
    # The new host is remembered: the second poll goes straight there.
    assert hosts == [fmip.DEFAULT_HOST, "p42-fmipmobile.icloud.com", "p42-fmipmobile.icloud.com"]


async def test_expired_token_is_renewed_once_and_the_request_retried() -> None:
    account = FakeApple()
    auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auths.append(request.headers["authorization"])
        if len(auths) == 1:
            return httpx.Response(401)
        return _ok(request)

    items = await _client(account, handler).fetch(locate=True)
    assert items
    assert account.renewals == 1
    assert auths[0] != auths[1]  # the retry used the renewed token


async def test_still_unauthorised_after_renewal_means_sign_in_again() -> None:
    account = FakeApple()
    with pytest.raises(AuthFailed):
        await _client(account, lambda request: httpx.Response(401)).fetch(locate=True)
    assert account.renewals == 1


async def test_expired_token_without_a_stored_password_means_sign_in_again() -> None:
    with pytest.raises(AuthFailed):
        await _client(FakeApple(password=None), lambda request: httpx.Response(401)).fetch(locate=True)


async def test_missing_token_means_sign_in_again() -> None:
    account = FakeApple()
    del account.service_tokens["mmeFMIPAppToken"]
    with pytest.raises(AuthFailed):
        await _client(account, _ok).fetch(locate=True)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_server_trouble_is_transient(status: int) -> None:
    with pytest.raises(Throttled):
        await _client(FakeApple(), lambda request: httpx.Response(status)).fetch(locate=True)


async def test_network_errors_are_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    with pytest.raises(Throttled):
        await _client(FakeApple(), handler).fetch(locate=True)


async def test_play_sound_posts_the_device_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    await _client(FakeApple(), handler).play_sound("iphone-1")
    assert seen[0].url.path.endswith("/playSound")
    assert b'"device":"iphone-1"' in seen[0].content.replace(b" ", b"")


def test_empty_account_has_no_devices() -> None:
    with pytest.raises(NoDevices):
        fmip.parse_devices({"content": []})


def test_family_members_are_named_as_owners() -> None:
    devices = load_devices()
    devices[0]["prsId"] = "kid"
    items = fmip.parse_devices({"content": devices, "userInfo": {"membersInfo": {"kid": {"firstName": "Sam"}}}})
    assert items["iphone-1"].owner == "Sam"
    assert items["ipad-1"].owner is None


def test_apple_root_certificate_is_bundled_and_loads() -> None:
    context = fmip._ssl_context()
    assert context.cert_store_stats()["x509_ca"] == 1
