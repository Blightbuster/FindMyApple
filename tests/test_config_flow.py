"""Config flow: sign-in, two-factor, device selection, errors, reauth, options."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.findmyapple.backends.icloud import AuthFailed, NotActivated, Throttled
from custom_components.findmyapple.const import (
    CONF_INCLUDE_FAMILY,
    CONF_REGION,
    CONF_REMEMBER_PASSWORD,
    CONF_TRACKED_DEVICES,
    DOMAIN,
)

USER_INPUT = {
    CONF_USERNAME: " Alex@Example.com ",
    CONF_PASSWORD: "hunter2",
    CONF_REGION: "global",
    CONF_REMEMBER_PASSWORD: True,
}


async def _start(hass: HomeAssistant, user_input: dict | None = None):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "icloud"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "icloud"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input or USER_INPUT)


async def test_sign_in_without_2fa_creates_entry(hass: HomeAssistant, fake_backend) -> None:
    result = await _start(hass)
    assert result["step_id"] == "select_devices"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TRACKED_DEVICES: ["iphone-1"], CONF_INCLUDE_FAMILY: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Alex@Example.com"
    assert result["data"] == {
        "kind": "icloud", CONF_USERNAME: "Alex@Example.com", CONF_REGION: "global", CONF_PASSWORD: "hunter2"
    }
    assert result["options"] == {CONF_TRACKED_DEVICES: ["iphone-1"], CONF_INCLUDE_FAMILY: False}
    assert result["result"].unique_id == "alex@example.com"


async def test_password_not_stored_when_not_remembered(hass: HomeAssistant, fake_backend) -> None:
    result = await _start(hass, {**USER_INPUT, CONF_REMEMBER_PASSWORD: False})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TRACKED_DEVICES: ["iphone-1"], CONF_INCLUDE_FAMILY: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_PASSWORD not in result["data"]


async def test_two_factor_wrong_then_right_code(hass: HomeAssistant, fake_backend) -> None:
    fake_backend.needs_2fa = True
    result = await _start(hass)
    assert result["step_id"] == "two_factor"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
    assert result["step_id"] == "two_factor"
    assert result["errors"] == {"base": "invalid_code"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123 456"})
    assert result["step_id"] == "select_devices"


async def test_security_key_accounts_abort(hass: HomeAssistant, fake_backend) -> None:
    fake_backend.needs_2fa = True
    fake_backend.security_key = True
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "security_key_unsupported"


async def test_bad_password_shows_error(hass: HomeAssistant, fake_backend) -> None:
    fake_backend.login_error = AuthFailed("Invalid email/password combination.")
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_throttled_shows_cannot_connect(hass: HomeAssistant, fake_backend) -> None:
    fake_backend.login_error = Throttled("503")
    result = await _start(hass)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_adp_without_web_access_aborts(hass: HomeAssistant, fake_backend) -> None:
    fake_backend.fetch_error = NotActivated("ZONE_NOT_FOUND")
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_activated"


async def test_same_account_twice_aborts(hass: HomeAssistant, fake_backend) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="alex@example.com", data={}).add_to_hass(hass)
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(hass: HomeAssistant, fake_backend) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="alex@example.com",
        title="Alex@Example.com",
        data={CONF_USERNAME: "Alex@Example.com", CONF_REGION: "global", CONF_PASSWORD: "old"},
        options={CONF_TRACKED_DEVICES: ["iphone-1"]},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    fake_backend.needs_2fa = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new", CONF_REMEMBER_PASSWORD: True}
    )
    assert result["step_id"] == "two_factor"
    fake_backend.needs_2fa = False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"


async def test_options_reject_min_above_max(hass: HomeAssistant, fake_backend) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="alex@example.com",
        data={CONF_USERNAME: "Alex@Example.com", CONF_REGION: "global", CONF_PASSWORD: "pw"},
        options={CONF_TRACKED_DEVICES: ["iphone-1"], CONF_INCLUDE_FAMILY: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    base = {CONF_TRACKED_DEVICES: ["iphone-1", "ipad-1"], CONF_INCLUDE_FAMILY: True, "home_interval": 15}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "away_min_interval": 40, "away_max_interval": 30}
    )
    assert result["errors"] == {"away_min_interval": "min_above_max"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "away_min_interval": 5, "away_max_interval": 30}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TRACKED_DEVICES] == ["iphone-1", "ipad-1"]
