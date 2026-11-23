"""Setup, entities, polling behaviour and failure handling."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.findmyapple.backends.icloud import AuthFailed, Throttled
from custom_components.findmyapple.const import (
    BACKOFF_START,
    CONF_INCLUDE_FAMILY,
    CONF_REGION,
    CONF_TRACKED_DEVICES,
    DOMAIN,
)


def _entry(tracked: list[str] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="alex@example.com",
        title="Alex@Example.com",
        data={CONF_USERNAME: "Alex@Example.com", CONF_REGION: "global", CONF_PASSWORD: "pw"},
        options={CONF_TRACKED_DEVICES: tracked or ["iphone-1", "ipad-1"], CONF_INCLUDE_FAMILY: True},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_creates_entities_for_tracked_devices_only(hass: HomeAssistant, fake_backend) -> None:
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

    # Untracked AirPods: no entities. iPad without location: tracker exists, position unknown.
    registry = er.async_get(hass)
    assert not [e for e in registry.entities.values() if e.unique_id.startswith("airpods-1")]
    assert hass.states.get("device_tracker.living_room_ipad").state == "unknown"

    # Every poll asks for a fresh location.
    assert fake_backend.instances[-1].fetch_calls == [True]


async def test_play_sound_button(hass: HomeAssistant, fake_backend) -> None:
    await _setup(hass, _entry())
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.alex_s_iphone_play_sound"}, blocking=True
    )
    assert fake_backend.instances[-1].sounds == ["iphone-1"]


async def test_auth_failure_starts_reauth(hass: HomeAssistant, fake_backend) -> None:
    fake_backend.login_error = AuthFailed("No session token available")
    entry = _entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_throttling_backs_off_and_keeps_last_location(hass: HomeAssistant, fake_backend, freezer) -> None:
    entry = _entry()
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator

    fake_backend.fetch_error = Throttled("503")
    freezer.tick(timedelta(hours=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.update_interval == BACKOFF_START
    assert not coordinator.last_update_success

    freezer.tick(BACKOFF_START)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.update_interval == BACKOFF_START * 2

    # Recovery resets the interval to the adaptive one.
    fake_backend.fetch_error = None
    freezer.tick(BACKOFF_START * 2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.last_update_success
    assert coordinator.update_interval != BACKOFF_START * 2
    assert hass.states.get("device_tracker.alex_s_iphone").attributes["latitude"] == 52.52


async def test_locations_restored_after_restart(hass: HomeAssistant, fake_backend) -> None:
    entry = _entry()
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)

    # Apple now returns no location at all; the restored one is kept.
    for device in fake_backend.devices:
        device["location"] = None
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    tracker = hass.states.get("device_tracker.alex_s_iphone")
    assert tracker.attributes["latitude"] == 52.52
    assert tracker.attributes["is_old"] is True


async def test_diagnostics_redacts_location_and_identity(hass: HomeAssistant, fake_backend) -> None:
    from custom_components.findmyapple.diagnostics import async_get_config_entry_diagnostics

    entry = _entry()
    await _setup(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert "pw" not in text.split("'password': ")[-1][:6]
    assert "52.52" not in text
    assert "Alex@Example.com" not in text
    assert diag["devices"][0]["raw"]["location"]["latitude"] == "**REDACTED**"


async def test_removing_entry_deletes_session(hass: HomeAssistant, fake_backend) -> None:
    from custom_components.findmyapple import session_dir

    entry = _entry()
    await _setup(hass, entry)
    path = session_dir(hass, entry.unique_id)
    await hass.async_add_executor_job(lambda: (path.mkdir(parents=True, exist_ok=True), (path / "s").write_text("t")))
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not await hass.async_add_executor_job(path.exists)
