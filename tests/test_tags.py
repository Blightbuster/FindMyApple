"""Find My network tags: stale-tag probe, error mapping, state round trip."""

from __future__ import annotations

from datetime import timedelta

import pytest
from findmy import FindMyAccessory
from findmy.errors import GrandSlamRefusedError, UnauthorizedError

from custom_components.findmyapple.backends import findmy_network as fn
from custom_components.findmyapple.backends.icloud import AuthFailed

from .conftest import NOW, FakeAccount, FakeReport, make_accessory
from .testdata import TAG_ACCURACY, TAG_LATITUDE, TAG_LONGITUDE

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


async def test_emoji_is_set_on_the_located_tag_and_survives_a_restart() -> None:
    tag = make_accessory("Keys", "A", aligned=NOW - timedelta(hours=2), index=5000)
    session = fn.TagSession(FakeAccount(), {"A": tag}, {"A": "🔑"})
    assert (await session.fetch(["A"], now=NOW))["A"].emoji == "🔑"
    assert session.to_state()["emojis"] == {"A": "🔑"}  # saved with the session, restored by from_state
    assert fn.TagSession(FakeAccount(), {"A": tag}).emojis == {}  # sessions saved before emoji existed have none


class _Store:
    async def fetch_records(self):
        return ["encrypted"]

    async def zone_record_defaults(self, keys):
        return {}


class _Client:
    store = _Store()

    async def zone_keys(self):
        return ["zone-key"]


async def test_emoji_come_from_the_naming_records(monkeypatch) -> None:
    from types import SimpleNamespace

    import findmy.cloudkit.beacons as beacons

    def group(name, values):
        naming = None if values is None else SimpleNamespace(values=values)
        return SimpleNamespace(beacon=SimpleNamespace(name=name), naming=naming)

    monkeypatch.setattr(beacons, "decrypt_records", lambda records, keys, default_master_keys: ["decrypted"])
    monkeypatch.setattr(
        beacons,
        "group_records",
        lambda records: [
            group("T1", {"emoji": " 🔑 "}),
            group("T2", None),
            group("T3", {}),
            group("T4", {"emoji": ""}),
        ],
    )
    assert await fn.fetch_emojis(_Client()) == {"T1": "🔑"}  # tags without an emoji are simply absent


async def test_failing_to_read_emoji_is_not_fatal() -> None:
    class Broken:
        async def zone_keys(self):
            raise RuntimeError("zone unavailable")

    assert await fn.fetch_emojis(Broken()) == {}
