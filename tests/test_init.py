"""Setup, entities, polling behaviour and failure handling for the single-login account entry."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.findmyapple import account_state_store
from custom_components.findmyapple.backends.icloud import AuthFailed, Throttled
from custom_components.findmyapple.const import (
    BACKOFF_START,
    CONF_INCLUDE_FAMILY,
    CONF_TAG_INTERVAL,
    CONF_TRACKED_ITEMS,
    DOMAIN,
)

from .conftest import Apple
from .testdata import TAG_LATITUDE, TAG_SLUG


def _entry(tracked: list[str] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="alex@example.com",
        title="Alex@Example.com",
        data={CONF_USERNAME: "Alex@Example.com"},
        options={
            CONF_TRACKED_ITEMS: tracked or ["iphone-1", "ipad-1", "T1", "P1"],
            CONF_INCLUDE_FAMILY: True,
            CONF_TAG_INTERVAL: 15,
        },
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, *, saved: bool = True) -> None:
    entry.add_to_hass(hass)
    if saved:
        await account_state_store(hass, entry.unique_id).async_save({"account": {"type": "account"}, "accessories": []})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _tick(hass: HomeAssistant, freezer, delta: timedelta) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_creates_entities_for_devices_and_tags(hass: HomeAssistant, apple: Apple) -> None:
    hass.config.latitude, hass.config.longitude = 52.50, 13.40
    entry = _entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    tracker = hass.states.get("device_tracker.alex_s_iphone")
    assert tracker is not None
    assert tracker.attributes["latitude"] == 52.52
    assert tracker.attributes["gps_accuracy"] == 12.5
    assert tracker.attributes["battery_level"] == 87
    assert tracker.attributes["source"] == "icloud"
    assert hass.states.get("sensor.alex_s_iphone_battery").state == "87"
    assert hass.states.get("binary_sensor.alex_s_iphone_charging").state == "on"
    assert float(hass.states.get("sensor.alex_s_iphone_distance_from_home").state) > 2
    assert hass.states.get("button.alex_s_iphone_play_sound") is not None

    # The tag comes from the same session: tracker and last-seen, but no battery or sound.
    tag = hass.states.get(f"device_tracker.{TAG_SLUG}")
    assert tag.attributes["latitude"] == TAG_LATITUDE
    assert tag.attributes["source"] == "findmy_network"
    assert hass.states.get(f"sensor.{TAG_SLUG}_battery") is None
    assert hass.states.get(f"button.{TAG_SLUG}_play_sound") is None

    # The account's own iPhone is in the keychain as well, but FMIP covers it: never listed twice.
    registry = er.async_get(hass)
    assert not [e for e in registry.entities.values() if e.unique_id.startswith("P1")]
    assert apple.session.fetches == [["T1"]]
    # Untracked devices get no entities. iPad without location: tracker exists, position unknown.
    assert hass.states.get("device_tracker.living_room_ipad").state == "unknown"
    assert not [e for e in registry.entities.values() if e.unique_id.startswith("airpods-1")]
    assert apple.fmip.instances[-1].fetch_calls == [True]


async def test_tag_emoji_becomes_the_picture_and_devices_get_type_icons(hass: HomeAssistant, apple: Apple) -> None:
    await _setup(hass, _entry())
    tag = hass.states.get(f"device_tracker.{TAG_SLUG}")
    assert tag.attributes["entity_picture"].startswith("data:image/svg+xml")
    assert "%F0%9F%94%91" in tag.attributes["entity_picture"]  # the key emoji, URL-encoded
    assert tag.attributes["icon"] == "mdi:tag"
    phone = hass.states.get("device_tracker.alex_s_iphone")
    assert phone.attributes["icon"] == "mdi:cellphone"
    assert "entity_picture" not in phone.attributes  # no emoji: the icon shows


async def test_tag_without_emoji_keeps_the_tag_icon(hass: HomeAssistant, apple: Apple) -> None:
    apple.session.emojis = {}
    await _setup(hass, _entry())
    tag = hass.states.get(f"device_tracker.{TAG_SLUG}")
    assert "entity_picture" not in tag.attributes
    assert tag.attributes["icon"] == "mdi:tag"


async def test_untracked_tag_is_not_fetched(hass: HomeAssistant, apple: Apple) -> None:
    await _setup(hass, _entry(["iphone-1"]))
    assert apple.session.fetches == []
    assert hass.states.get(f"device_tracker.{TAG_SLUG}") is None


async def test_tags_are_polled_at_their_own_interval(hass: HomeAssistant, apple: Apple, freezer) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "home_interval": 5, "away_min_interval": 5, "away_max_interval": 5}
    )
    await _setup(hass, entry)
    # Devices every 5 minutes, tags only every 15.
    for _ in range(6):
        await _tick(hass, freezer, timedelta(minutes=5))
    assert len(apple.fmip.instances[-1].fetch_calls) >= 6
    assert len(apple.session.fetches) == 3
    assert hass.states.get(f"device_tracker.{TAG_SLUG}").attributes["latitude"] == TAG_LATITUDE


async def test_play_sound_button(hass: HomeAssistant, apple: Apple) -> None:
    await _setup(hass, _entry())
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.alex_s_iphone_play_sound"}, blocking=True
    )
    assert apple.fmip.instances[-1].sounds == ["iphone-1"]


async def test_auth_failure_starts_reauth(hass: HomeAssistant, apple: Apple) -> None:
    apple.fmip.error = AuthFailed("FMIP refused the session (HTTP 401)")
    entry = _entry(["iphone-1"])
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_devices_down_but_tags_still_update(hass: HomeAssistant, apple: Apple, freezer) -> None:
    entry = _entry()
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator

    apple.fmip.error = Throttled("503")
    await _tick(hass, freezer, timedelta(hours=1))
    # One half failing keeps the other half and the last known device positions, and backs off.
    assert coordinator.last_update_success
    assert coordinator.update_interval == BACKOFF_START
    assert hass.states.get("device_tracker.alex_s_iphone").attributes["latitude"] == 52.52
    assert hass.states.get(f"device_tracker.{TAG_SLUG}").attributes["latitude"] == TAG_LATITUDE

    # Recovery returns to the adaptive interval.
    apple.fmip.error = None
    await _tick(hass, freezer, BACKOFF_START)
    assert coordinator.update_interval != BACKOFF_START


async def test_repeated_failures_back_off_exponentially(hass: HomeAssistant, apple: Apple, freezer) -> None:
    entry = _entry(["iphone-1"])
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    apple.fmip.error = Throttled("503")
    await _tick(hass, freezer, timedelta(hours=1))
    assert coordinator.update_interval == BACKOFF_START
    await _tick(hass, freezer, BACKOFF_START)
    assert coordinator.update_interval == BACKOFF_START * 2
    assert hass.states.get("device_tracker.alex_s_iphone").attributes["latitude"] == 52.52


async def test_locations_restored_after_restart(hass: HomeAssistant, apple: Apple) -> None:
    entry = _entry()
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)

    # Apple now returns no location at all; the restored one is kept.
    for device in apple.fmip.devices:
        device["location"] = None
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    tracker = hass.states.get("device_tracker.alex_s_iphone")
    assert tracker.attributes["latitude"] == 52.52
    assert tracker.attributes["is_old"] is True


async def test_entry_without_saved_session_asks_to_sign_in(hass: HomeAssistant, apple: Apple) -> None:
    entry = _entry()
    await _setup(hass, entry, saved=False)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_diagnostics_redacts_location_and_identity(hass: HomeAssistant, apple: Apple) -> None:
    from custom_components.findmyapple.diagnostics import async_get_config_entry_diagnostics

    entry = _entry()
    await _setup(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert "52.52" not in text
    assert "Alex@Example.com" not in text
    assert any(d["raw"].get("location", {}).get("latitude") == "**REDACTED**" for d in diag["devices"])


async def test_removing_entry_deletes_saved_session(hass: HomeAssistant, apple: Apple) -> None:
    entry = _entry()
    await _setup(hass, entry)
    assert await account_state_store(hass, entry.unique_id).async_load() is not None
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert await account_state_store(hass, entry.unique_id).async_load() is None
