"""Config flow: one sign-in, two-factor, keychain unlock, one list of devices and tags, errors, reauth, options."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.findmyapple import account_state_store
from custom_components.findmyapple.backends import findmy_network as fn
from custom_components.findmyapple.backends.icloud import NoDevices, Throttled
from custom_components.findmyapple.const import (
    CONF_INCLUDE_FAMILY,
    CONF_TAG_INTERVAL,
    CONF_TRACKED_ITEMS,
    DOMAIN,
)

from .conftest import Apple
from .testdata import TAG_NAME

USER_INPUT = {CONF_USERNAME: " Alex@Example.com ", CONF_PASSWORD: "pw"}


async def _start(hass: HomeAssistant, user_input: dict | None = None):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    # One flow: straight to the sign-in form, no "devices or tags?" menu.
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input or USER_INPUT)


async def _through_2fa(hass: HomeAssistant, result):
    assert result["step_id"] == "code"  # one 2FA method: requested straight away
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123 456"})


def _select_options(result) -> dict[str, str]:
    schema = result["data_schema"].schema
    key = next(k for k in schema if k == CONF_TRACKED_ITEMS)
    return {o["value"]: o["label"] for o in schema[key].config["options"]}


async def test_one_login_covers_devices_and_tags(hass: HomeAssistant, apple: Apple) -> None:
    with patch("custom_components.findmyapple.async_setup_entry", return_value=True):
        result = await _start(hass)
        assert apple.accounts[-1].method.requested
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
        assert result["errors"] == {"base": "invalid_code"}
        result = await _through_2fa(hass, result)
        assert result["step_id"] == "unlock"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"record": "1", "passcode": "0000"})
        assert result["errors"] == {"base": "invalid_passcode"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"record": "1", "passcode": "1234"})
        assert result["step_id"] == "select"

        # Devices (FMIP) and tags (keychain) in one list. Own Apple devices from the keychain aren't repeated.
        labels = _select_options(result)
        assert "iphone-1" in labels and "T1" in labels and "P1" not in labels
        assert labels["T1"].startswith(TAG_NAME)
        key = next(k for k in result["data_schema"].schema if k == CONF_TRACKED_ITEMS)
        assert {"T1", "iphone-1", "airpods-1"} <= set(key.default())

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TRACKED_ITEMS: ["iphone-1", "T1"], CONF_INCLUDE_FAMILY: False}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Alex@Example.com"
    assert result["data"] == {CONF_USERNAME: "Alex@Example.com"}
    assert result["options"] == {
        CONF_TRACKED_ITEMS: ["iphone-1", "T1"],
        CONF_INCLUDE_FAMILY: False,
        CONF_TAG_INTERVAL: 15,
    }
    assert result["result"].unique_id == "alex@example.com"
    stored = await account_state_store(hass, "alex@example.com").async_load()
    assert stored["account"] == {"type": "account", "fake": True}
    assert len(stored["accessories"]) == 2
    assert apple.fmip.instances[0].fetch_calls == [False]  # listing devices must not wake their GPS


async def test_no_keychain_escrow_still_sets_up_devices(hass: HomeAssistant, apple: Apple) -> None:
    apple.keychain.escrow = ()
    result = await _through_2fa(hass, await _start(hass))
    assert result["step_id"] == "select"
    labels = _select_options(result)
    assert "iphone-1" in labels and "T1" not in labels


async def test_wrong_password_shows_error(hass: HomeAssistant, apple: Apple) -> None:
    result = await _start(hass, {**USER_INPUT, CONF_PASSWORD: "nope"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_stops_after_three_wrong_passcodes(hass: HomeAssistant, apple: Apple) -> None:
    result = await _through_2fa(hass, await _start(hass))
    for _ in range(2):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"record": "0", "passcode": "x"})
        assert result["errors"] == {"base": "invalid_passcode"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"record": "0", "passcode": "x"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "too_many_passcode_attempts"
    assert apple.keychain.unlock_attempts == 3


async def test_apple_without_find_my_tokens_aborts(hass: HomeAssistant, apple: Apple) -> None:
    original = fn.new_account

    def without_fmip_token(provider):
        account = original(provider)
        account.service_tokens.pop("mmeFMIPAppToken")
        return account

    with patch.object(fn, "new_account", without_fmip_token):
        result = await _through_2fa(hass, await _start(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_findmy_token"


async def test_device_list_failure_aborts(hass: HomeAssistant, apple: Apple) -> None:
    apple.fmip.error = Throttled("503")
    result = await _through_2fa(hass, await _start(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_account_without_devices_or_tags_aborts(hass: HomeAssistant, apple: Apple) -> None:
    apple.fmip.error = NoDevices("none")
    apple.keychain.escrow = ()
    result = await _through_2fa(hass, await _start(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


async def test_same_account_twice_aborts(hass: HomeAssistant, apple: Apple) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="alex@example.com", data={}).add_to_hass(hass)
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_replaces_the_session_and_keeps_tag_keys(hass: HomeAssistant, apple: Apple) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="alex@example.com",
        title="Alex@Example.com",
        data={CONF_USERNAME: "Alex@Example.com"},
        options={CONF_TRACKED_ITEMS: ["iphone-1", "T1"]},
    )
    entry.add_to_hass(hass)
    await account_state_store(hass, entry.unique_id).async_save({"account": {"old": True}, "accessories": [{"k": 1}]})
    with patch("custom_components.findmyapple.async_setup_entry", return_value=True):
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "pw"})
        result = await _through_2fa(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    stored = await account_state_store(hass, entry.unique_id).async_load()
    assert stored["account"] == {"type": "account", "fake": True}
    assert stored["accessories"] == [{"k": 1}]


async def test_options_reject_min_above_max(hass: HomeAssistant, apple: Apple) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="alex@example.com",
        data={CONF_USERNAME: "Alex@Example.com"},
        options={CONF_TRACKED_ITEMS: ["iphone-1"], CONF_INCLUDE_FAMILY: True},
    )
    entry.add_to_hass(hass)
    await account_state_store(hass, entry.unique_id).async_save({"account": {"type": "account"}, "accessories": []})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    base = {
        CONF_TRACKED_ITEMS: ["iphone-1", "ipad-1"],
        CONF_INCLUDE_FAMILY: True,
        "home_interval": 15,
        CONF_TAG_INTERVAL: 15,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "away_min_interval": 40, "away_max_interval": 30}
    )
    assert result["errors"] == {"away_min_interval": "min_above_max"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "away_min_interval": 5, "away_max_interval": 30}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TRACKED_ITEMS] == ["iphone-1", "ipad-1"]
