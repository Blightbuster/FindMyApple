"""Pure logic: payload normalisation, keeping the last good location, polling intervals, CoreADI checks."""

from __future__ import annotations

import gzip
import struct
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.findmyapple import coreadi
from custom_components.findmyapple.backends.icloud import parse_device
from custom_components.findmyapple.coordinator import interval_for_distance, nearest_zone_km
from custom_components.findmyapple.models import TrackedItem

from .conftest import load_devices


def _by_id() -> dict:
    return {d["id"]: d for d in load_devices()}


def test_parse_full_device() -> None:
    item = parse_device(_by_id()["iphone-1"], owner=None)
    assert item.latitude == 52.52 and item.longitude == 13.405
    assert item.accuracy == 12.5
    assert item.timestamp == datetime.fromtimestamp(1795000000, tz=UTC)
    assert item.battery_level == 87
    assert item.battery_charging is True
    assert item.online is True
    assert item.can_play_sound is True
    assert item.model_display_name == "iPhone 16 Pro"


def test_parse_device_without_location_or_battery() -> None:
    item = parse_device(_by_id()["ipad-1"])
    assert not item.has_location
    assert item.timestamp is None
    assert item.battery_level is None  # 0.0 + "Unknown" means "not reported", not empty
    assert item.battery_charging is None
    assert item.online is False


def test_parse_old_location_and_family_owner() -> None:
    item = parse_device(_by_id()["airpods-1"], owner="Sam")
    assert item.is_old is True
    assert item.owner == "Sam"
    assert item.battery_charging is False
    assert item.can_play_sound is False


def _item(lat: float | None, ts: int | None) -> TrackedItem:
    return TrackedItem(
        id="x",
        name="x",
        source="icloud",
        latitude=lat,
        longitude=lat,
        timestamp=datetime.fromtimestamp(ts, tz=UTC) if ts else None,
    )


def test_merge_keeps_previous_when_new_location_missing() -> None:
    merged = _item(None, None).merge_previous(_item(1.0, 100))
    assert merged.latitude == 1.0 and merged.is_old


def test_merge_keeps_previous_when_new_location_older() -> None:
    merged = _item(2.0, 50).merge_previous(_item(1.0, 100))
    assert merged.latitude == 1.0


def test_merge_takes_newer_location() -> None:
    merged = _item(2.0, 200).merge_previous(_item(1.0, 100))
    assert merged.latitude == 2.0 and not merged.is_old


def test_storage_round_trip() -> None:
    item = parse_device(_by_id()["iphone-1"])
    restored = TrackedItem.from_storage(item.to_storage())
    assert (restored.latitude, restored.longitude, restored.timestamp) == (
        item.latitude,
        item.longitude,
        item.timestamp,
    )
    assert restored.is_old  # restored data is never presented as fresh


HOME, AWAY_MIN, AWAY_MAX = timedelta(minutes=15), timedelta(minutes=5), timedelta(minutes=30)


@pytest.mark.parametrize(
    ("km", "expected"),
    [(0, HOME), (0.001, AWAY_MIN), (25, timedelta(minutes=17, seconds=30)), (50, AWAY_MAX), (5000, AWAY_MAX)],
)
def test_interval_for_distance(km: float, expected: timedelta) -> None:
    assert abs(interval_for_distance(km, HOME, AWAY_MIN, AWAY_MAX) - expected) < timedelta(seconds=1)


def test_nearest_zone_is_zero_inside_radius() -> None:
    item = _item(None, None)
    item.latitude, item.longitude = 52.52, 13.405
    assert nearest_zone_km(item, [(52.5201, 13.405, 100)]) == 0
    assert nearest_zone_km(item, [(52.62, 13.405, 100)]) == pytest.approx(11.0, abs=0.2)


def _cpio_entry(name: str, data: bytes) -> bytes:
    encoded = name.encode() + b"\0"
    header = (
        b"070707" + b"0" * 53 + f"{len(encoded):06o}".encode() + f"{len(data):011o}".encode()
    )
    assert len(header) == 76
    return header + encoded + data


def _fat(slices: dict[int, bytes]) -> bytes:
    head = b"\xca\xfe\xba\xbe" + struct.pack(">I", len(slices))
    offset = 8 + 20 * len(slices)
    table, body = b"", b""
    for cpu, data in slices.items():
        table += struct.pack(">5I", cpu, 0, offset + len(body), len(data), 0)
        body += data
    return head + table + body


def test_coreadi_extracts_x86_slice_from_cpio() -> None:
    fat = _fat({0x7: b"i386-code", 0x01000007: b"x86_64-code"})
    cpio = _cpio_entry("./other", b"zz") + _cpio_entry("./" + coreadi.FRAMEWORK_PATH, fat) + _cpio_entry(
        "TRAILER!!!", b""
    )
    assert coreadi._slice(coreadi._file_from_cpio(cpio, coreadi.FRAMEWORK_PATH), 0x01000007) == b"x86_64-code"


def test_coreadi_rejects_unexpected_payload() -> None:
    with pytest.raises(coreadi.CoreAdiError, match="not the expected"):
        coreadi.extract(gzip.compress(b"not apple's bytes"))
