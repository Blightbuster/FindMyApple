"""Find My network tags: stale-tag probe, error mapping, config flow, setup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from findmy import FindMyAccessory, LoginState
from findmy.errors import GrandSlamRefusedError, UnauthorizedError
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.findmyapple import tag_state_store
from custom_components.findmyapple.backends import findmy_network as fn
from custom_components.findmyapple.backends.icloud import AuthFailed
from custom_components.findmyapple.const import CONF_KIND, CONF_TAG_INTERVAL, CONF_TRACKED_TAGS, DOMAIN, KIND_TAGS

from .testdata import TAG_ACCURACY, TAG_LATITUDE, TAG_LONGITUDE, TAG_MODEL, TAG_NAME, TAG_SLUG

NOW = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)


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
        self.service_tokens = {"searchPartyToken": "t"}

    async def fetch_location(self, keys: list[Any]) -> dict[Any, Any]:
        self.calls.append(list(keys))
        if self.error is not None:
            raise self.error
        return {k: self.reports.get(k) for k in keys}

    def to_json(self) -> dict[str, Any]:
        return {"type": "account", "fake": True}

    async def close(self) -> None:
        pass


# -- TagSession -----------------------------------------------------------------------------------


async def test_fresh_tag_is_located_with_one_batched_fetch() -> None:
    tag = make_accessory("Keys", "A", aligned=NOW - timedelta(hours=2), index=5000)
    account = FakeAccount({tag: FakeReport(TAG_LATITUDE, TAG_LONGITUDE, TAG_ACCURACY, NOW - timedelta(minutes=16))})
    session = fn.TagSession(account, {"A": tag})
    items = await session.fetch(["A"], now=NOW)
    assert account.calls == [[tag]]
    item = items["A"]
    assert (item.latitude, item.longitude, item.accuracy) == (TAG_LATITUDE, TAG_LONGITUDE, TAG_ACCURACY)
    assert item.source == "findmy_network"


async def test_stale_tag_is_probed_with_daily_keys_then_located() -> None:
    aligned = NOW - timedelta(days=137)  # like the July tags: ~13,000 primary indices of uncertainty
    tag = make_accessory("Car", "B", aligned=aligned, index=10000)
    session = fn.TagSession(FakeAccount(), {"B": tag})
    assert session.is_stale(tag, NOW)

    # Apple has a report under the daily key that was active ~120 days after the alignment.
    hit_secondary = (10000 + 120 * 96) // 96 + 1
    hit_key = tag._secondary_gen[hit_secondary]
    seen = NOW - timedelta(hours=3)
    session.account.reports = {hit_key: FakeReport(TAG_LATITUDE, TAG_LONGITUDE, TAG_ACCURACY, seen)}

    items = await session.fetch(["B"], now=NOW)
    probe_keys = session.account.calls[0]
    assert len(session.account.calls) == 2  # one probe request, then one normal fetch
    assert 100 < len(probe_keys) < 300  # daily keys only, not 13,000 primary ones
    assert not session.is_stale(tag, NOW)  # re-aligned from the probe hit
    assert session.account.calls[1] == [tag]
    assert "B" in items


async def test_stale_tag_without_reports_is_probed_at_most_daily() -> None:
    tag = make_accessory("Wallet", "C", aligned=NOW - timedelta(days=137), index=10000)
    session = fn.TagSession(FakeAccount(), {"C": tag})
    await session.fetch(["C"], now=NOW)
    await session.fetch(["C"], now=NOW + timedelta(minutes=15))
    assert len(session.account.calls) == 1  # second poll: no probe, and no 13,000-key scan
    await session.fetch(["C"], now=NOW + timedelta(hours=25))
    assert len(session.account.calls) == 2


async def test_untracked_tags_are_not_fetched() -> None:
    tag_a = make_accessory("A", "A", aligned=NOW, index=1)
    tag_b = make_accessory("B", "B", aligned=NOW, index=1)
    session = fn.TagSession(FakeAccount(), {"A": tag_a, "B": tag_b})
    items = await session.fetch(["A"], now=NOW)
    assert list(items) == ["A"]
    assert session.account.calls == [[tag_a]]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (UnauthorizedError("401"), AuthFailed),
        (GrandSlamRefusedError("init", -80009, "MID is invalid."), fn.MachineError),
        (GrandSlamRefusedError("complete", -22406, "Enter the correct password"), AuthFailed),
        (OSError("network down"), fn.Throttled),
    ],
)
async def test_errors_are_classified(error: Exception, expected: type[Exception]) -> None:
    tag = make_accessory("A", "A", aligned=NOW, index=1)
    session = fn.TagSession(FakeAccount(error=error), {"A": tag})
    with pytest.raises(expected):
        await session.fetch(["A"], now=NOW)


async def test_state_round_trip_keeps_alignment() -> None:
    tag = make_accessory("A", "A", aligned=NOW, index=1234)
    session = fn.TagSession(FakeAccount(), {"A": tag})
    state = session.to_state()
    restored = FindMyAccessory.from_json(state["accessories"][0])
    assert restored.get_min_index(NOW) == 1234


def test_apple_devices_are_recognised() -> None:
    assert fn.TagInfo("x", "Phone", "iPhone17,1").is_apple_device
    assert not fn.TagInfo("x", "Keys", "PAD Tag").is_apple_device


# -- config flow ----------------------------------------------------------------------------------


class FakeMethod:
    phone_number = "+1 ••••00"

    def __init__(self, account: FakeAccount) -> None:
        self.account = account
        self.requested = False

    async def request(self) -> None:
        self.requested = True

    async def submit(self, code: str) -> LoginState:
        return LoginState.LOGGED_IN if code == "123456" else LoginState.REQUIRE_2FA


@dataclass
class FakeRecord:
    label: str

    def describe(self) -> str:
        return self.label


class FakeOptions:
    recoverable = (FakeRecord("Old iPhone"), FakeRecord("iPhone 16 Pro"))


class FakeClient:
    passcode = "1234"

    def __init__(self) -> None:
        self.unlock_attempts = 0

    async def recovery_options(self) -> FakeOptions:
        return FakeOptions()

    async def unlock(self, record: FakeRecord, passcode: str) -> list:
        self.unlock_attempts += 1
        if passcode != self.passcode:
            raise RuntimeError("Escrow proxy rejected recover")
        return ["key"]

    async def accessories(self) -> list[FindMyAccessory]:
        return [
            make_accessory(TAG_NAME, "T1", aligned=NOW, index=208, model=TAG_MODEL),
            make_accessory("iPhone", "P1", aligned=NOW, index=1, model="iPhone17,1"),
        ]

    async def close(self) -> None:
        pass


class FakeFlowAccount(FakeAccount):
    def __init__(self) -> None:
        super().__init__()
        self.login_state = LoginState.LOGGED_OUT
        self.method = FakeMethod(self)

    async def login(self, username: str, password: str) -> LoginState:
        if password != "pw":
            raise fn.InvalidCredentialsError("bad")
        return LoginState.REQUIRE_2FA

    async def get_2fa_methods(self) -> list[FakeMethod]:
        return [self.method]


@pytest.fixture
def tag_flow_mocks():
    client = FakeClient()
    accounts: list[FakeFlowAccount] = []

    async def open_machine(root, *, provision):
        return object()

    def new_account(provider):
        account = FakeFlowAccount()
        accounts.append(account)
        return account

    async def open_client(account):
        return client

    with (
        patch.object(fn, "async_open_machine", open_machine),
        patch.object(fn, "new_account", new_account),
        patch("findmy.icloud.AsyncFindMyClient.open", open_client),
        # The flow is under test, not the entry it creates: don't set that up for real.
        patch("custom_components.findmyapple.async_setup_entry", return_value=True),
    ):
        yield client, accounts


async def _start_tags(hass: HomeAssistant, password: str = "pw"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "tags"})
    assert result["step_id"] == "tags"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "Alex@Example.com", CONF_PASSWORD: password}
    )


async def test_tags_flow_end_to_end(hass: HomeAssistant, tag_flow_mocks) -> None:
    _client, accounts = tag_flow_mocks
    result = await _start_tags(hass)
    assert result["step_id"] == "tags_code"  # one 2FA method: requested straight away
    assert accounts[-1].method.requested

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
    assert result["errors"] == {"base": "invalid_code"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123 456"})
    assert result["step_id"] == "tags_unlock"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"record": "1", "passcode": "0000"})
    assert result["errors"] == {"base": "invalid_passcode"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"record": "1", "passcode": "1234"})
    assert result["step_id"] == "tags_select"
    # Apple devices aren't pre-selected: the iCloud option tracks them better.
    schema_default = next(k for k in result["data_schema"].schema if k == CONF_TRACKED_TAGS).default()
    assert schema_default == ["T1"]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TRACKED_TAGS: ["T1"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_KIND: KIND_TAGS, CONF_USERNAME: "Alex@Example.com"}
    assert CONF_PASSWORD not in result["data"]  # lives in the private tag store, with the session
    assert result["result"].unique_id == "alex@example.com#tags"
    stored = await tag_state_store(hass, "alex@example.com#tags").async_load()
    assert stored["account"] == {"type": "account", "fake": True}
    assert len(stored["accessories"]) == 2


async def test_tags_flow_wrong_password(hass: HomeAssistant, tag_flow_mocks) -> None:
    result = await _start_tags(hass, password="nope")
    assert result["errors"] == {"base": "invalid_auth"}


async def test_tags_flow_stops_after_three_wrong_passcodes(hass: HomeAssistant, tag_flow_mocks) -> None:
    client, _ = tag_flow_mocks
    result = await _start_tags(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    for _ in range(2):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"record": "0", "passcode": "x"})
        assert result["errors"] == {"base": "invalid_passcode"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"record": "0", "passcode": "x"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "too_many_passcode_attempts"
    assert client.unlock_attempts == 3


# -- setup ----------------------------------------------------------------------------------------


class FakeSession:
    def __init__(self) -> None:
        self.accessories = {"T1": make_accessory(TAG_NAME, "T1", aligned=NOW, index=208, model=TAG_MODEL)}
        self.fetches = 0
        self.error: Exception | None = None

    async def fetch(self, tracked, now=None):
        self.fetches += 1
        if self.error is not None:
            raise self.error
        report = FakeReport(TAG_LATITUDE, TAG_LONGITUDE, TAG_ACCURACY, datetime.now(UTC) - timedelta(minutes=16))
        return {i: fn.report_to_item(self.accessories[i], report) for i in tracked if i in self.accessories}

    def to_state(self):
        return {"account": {"type": "account"}, "accessories": []}

    async def close(self) -> None:
        pass


async def _setup_tags_entry(hass: HomeAssistant, session: FakeSession) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="alex@example.com#tags",
        title="Alex@Example.com (tags)",
        data={CONF_KIND: KIND_TAGS, CONF_USERNAME: "Alex@Example.com"},
        options={CONF_TRACKED_TAGS: ["T1"], CONF_TAG_INTERVAL: 15},
    )
    entry.add_to_hass(hass)
    await tag_state_store(hass, entry.unique_id).async_save({"account": {"type": "account"}, "accessories": []})

    async def open_machine(root, *, provision):
        assert provision is False  # setup must never provision a new machine
        return object()

    with (
        patch.object(fn, "async_open_machine", open_machine),
        patch.object(fn.TagSession, "from_state", lambda provider, state: session),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_tag_entry_creates_tracker_without_battery_or_sound(hass: HomeAssistant) -> None:
    session = FakeSession()
    entry = await _setup_tags_entry(hass, session)
    assert entry.state is ConfigEntryState.LOADED
    tracker = hass.states.get(f"device_tracker.{TAG_SLUG}")
    assert tracker is not None
    assert tracker.attributes["latitude"] == TAG_LATITUDE
    assert tracker.attributes["source"] == "findmy_network"
    assert hass.states.get(f"sensor.{TAG_SLUG}_last_seen") is not None
    assert hass.states.get(f"sensor.{TAG_SLUG}_battery") is None
    assert hass.states.get(f"binary_sensor.{TAG_SLUG}_charging") is None
    assert hass.states.get(f"button.{TAG_SLUG}_play_sound") is None
    assert hass.states.get(f"button.{TAG_SLUG}_refresh_location") is not None


async def test_tag_auth_failure_starts_reauth(hass: HomeAssistant) -> None:
    session = FakeSession()
    session.error = AuthFailed("401")
    entry = await _setup_tags_entry(hass, session)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_tag_entry_without_saved_session_asks_to_sign_in(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="sam@example.com#tags",
        data={CONF_KIND: KIND_TAGS, CONF_USERNAME: "sam@example.com"},
        options={CONF_TRACKED_TAGS: []},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
