"""Shared fixtures: fakes standing in for Apple (the FMIP service, the signed-in account, the keychain)."""

from __future__ import annotations

import json
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from findmy import FindMyAccessory, LoginState

from custom_components.findmyapple.backends import findmy_network as fn
from custom_components.findmyapple.backends.fmip import parse_devices
from custom_components.findmyapple.models import TrackedItem

from .testdata import TAG_ACCURACY, TAG_LATITUDE, TAG_LONGITUDE, TAG_MODEL, TAG_NAME

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)
TOKENS = {"searchPartyToken": "t", "mmeFMIPAppToken": "f"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


def load_devices() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "refresh_client.json").read_text(encoding="utf-8"))["content"]


def make_accessory(name: str, ident: str, *, aligned: datetime, index: int, model: str = "PAD Tag") -> FindMyAccessory:
    return FindMyAccessory(
        master_key=bytes.fromhex("0123456789abcdef" * 3 + "01234567"),
        skn=bytes(range(32)),
        sks=bytes(range(32, 64)),
        paired_at=datetime(2026, 1, 1, tzinfo=UTC),
        name=name,
        model=model,
        identifier=ident,
        alignment_date=aligned,
        alignment_index=index,
    )


@dataclass
class FakeReport:
    latitude: float
    longitude: float
    horizontal_accuracy: int
    timestamp: datetime
    confidence: int = 2
    status: int = 0


class FakeAccount:
    """Stands in for AsyncAppleAccount: records what fetch_location was asked for."""

    def __init__(self, reports: dict[Any, FakeReport] | None = None, error: Exception | None = None) -> None:
        self.login_state = LoginState.LOGGED_IN
        self.reports = reports or {}
        self.error = error
        self.calls: list[list[Any]] = []
        self.service_tokens = dict(TOKENS)

    async def fetch_location(self, keys: list[Any]) -> dict[Any, Any]:
        self.calls.append(list(keys))
        if self.error is not None:
            raise self.error
        return {k: self.reports.get(k) for k in keys}

    def to_json(self) -> dict[str, Any]:
        return {"type": "account", "fake": True}

    async def close(self) -> None:
        pass


class FakeFmip:
    """Behaves like FmipClient. Class attributes steer every instance a test creates."""

    error: Exception | None = None
    devices: ClassVar[list[dict[str, Any]]] = []
    instances: ClassVar[list[FakeFmip]] = []

    def __init__(self, account: Any) -> None:
        self.account = account
        self.fetch_calls: list[bool] = []
        self.sounds: list[str] = []
        FakeFmip.instances.append(self)

    async def fetch(self, *, locate: bool) -> dict[str, TrackedItem]:
        self.fetch_calls.append(locate)
        if FakeFmip.error is not None:
            raise FakeFmip.error
        return parse_devices({"content": FakeFmip.devices})

    async def play_sound(self, device_id: str) -> None:
        self.sounds.append(device_id)

    async def close(self) -> None:
        pass


class FakeSession:
    """Behaves like TagSession: one tag and one of the account's own iPhones (which must be ignored)."""

    def __init__(self) -> None:
        self.account = FakeAccount()
        self.accessories = {
            "T1": make_accessory(TAG_NAME, "T1", aligned=NOW, index=208, model=TAG_MODEL),
            "P1": make_accessory("Own iPhone", "P1", aligned=NOW, index=1, model="iPhone17,1"),
        }
        self.emojis = {"T1": "🔑"}
        self.fetches: list[list[str]] = []
        self.error: Exception | None = None

    async def fetch(self, tracked, now=None):
        self.fetches.append(list(tracked))
        if self.error is not None:
            raise self.error
        report = FakeReport(TAG_LATITUDE, TAG_LONGITUDE, TAG_ACCURACY, datetime.now(UTC) - timedelta(minutes=16))
        items = {i: fn.report_to_item(self.accessories[i], report) for i in tracked if i in self.accessories}
        for item_id, item in items.items():
            item.emoji = self.emojis.get(item_id)
        return items

    def to_state(self):
        return {"account": {"type": "account"}, "accessories": []}

    async def close(self) -> None:
        pass


@dataclass
class FakeRecord:
    label: str

    def describe(self) -> str:
        return self.label


class FakeOptions:
    def __init__(self, records: tuple[FakeRecord, ...]) -> None:
        self.recoverable = records


class FakeKeychain:
    """The iCloud keychain: `escrow` is what Apple holds, `passcode` unlocks it."""

    passcode = "1234"

    def __init__(self) -> None:
        self.escrow = (FakeRecord("Old iPhone"), FakeRecord("iPhone 16 Pro"))
        self.unlock_attempts = 0

    async def recovery_options(self) -> FakeOptions:
        return FakeOptions(self.escrow)

    async def unlock(self, record: FakeRecord, passcode: str) -> list:
        self.unlock_attempts += 1
        if passcode != self.passcode:
            raise RuntimeError("Escrow proxy rejected recover")
        return ["key"]

    async def accessories(self) -> list[FindMyAccessory]:
        return list(FakeSession().accessories.values())

    async def close(self) -> None:
        pass


class FakeMethod:
    phone_number = "+1 ••••00"

    def __init__(self) -> None:
        self.requested = False

    async def request(self) -> None:
        self.requested = True

    async def submit(self, code: str) -> LoginState:
        return LoginState.LOGGED_IN if code == "123456" else LoginState.REQUIRE_2FA


class FakeFlowAccount(FakeAccount):
    def __init__(self) -> None:
        super().__init__()
        self.login_state = LoginState.LOGGED_OUT
        self.method = FakeMethod()

    async def login(self, username: str, password: str) -> LoginState:
        if password != "pw":
            raise fn.InvalidCredentialsError("bad")
        return LoginState.REQUIRE_2FA

    async def get_2fa_methods(self) -> list[FakeMethod]:
        return [self.method]


@dataclass
class Apple:
    """Handles on everything the tests steer."""

    fmip: type[FakeFmip]
    keychain: FakeKeychain
    session: FakeSession
    accounts: list[FakeFlowAccount]


@pytest.fixture
def apple() -> Generator[Apple]:
    FakeFmip.error = None
    FakeFmip.devices = load_devices()
    FakeFmip.instances = []
    keychain = FakeKeychain()
    session = FakeSession()
    accounts: list[FakeFlowAccount] = []

    async def open_machine(root, *, provision):
        return object()

    def new_account(provider):
        account = FakeFlowAccount()
        accounts.append(account)
        return account

    async def open_keychain(account):
        return keychain

    async def read_emojis(client):
        return {"T1": "🔑"}

    with (
        patch("custom_components.findmyapple.backends.fmip.FmipClient", FakeFmip),
        patch.object(fn, "async_open_machine", open_machine),
        patch.object(fn, "new_account", new_account),
        patch.object(fn, "fetch_emojis", read_emojis),
        patch.object(fn.TagSession, "from_state", lambda provider, state: session),
        patch("findmy.icloud.AsyncFindMyClient.open", open_keychain),
    ):
        yield Apple(FakeFmip, keychain, session, accounts)
