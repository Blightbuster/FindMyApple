"""Config flow: one Apple sign-in (as the account's emulated Mac) for devices and Find My tags."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import FindMyAppleConfigEntry, account_state_store, machine_root
from .backends.icloud import AuthFailed, NoDevices, Throttled
from .const import (
    CONF_AWAY_MAX_INTERVAL,
    CONF_AWAY_MIN_INTERVAL,
    CONF_HOME_INTERVAL,
    CONF_INCLUDE_FAMILY,
    CONF_TAG_INTERVAL,
    CONF_TRACKED_ITEMS,
    DEFAULT_AWAY_MAX_INTERVAL,
    DEFAULT_AWAY_MIN_INTERVAL,
    DEFAULT_HOME_INTERVAL,
    DEFAULT_TAG_INTERVAL,
    DOMAIN,
)

if TYPE_CHECKING:
    from findmy import AsyncAppleAccount, FindMyAccessory
    from findmy.icloud import AsyncFindMyClient

    from .models import TrackedItem

_LOGGER = logging.getLogger(__name__)

CONF_CODE = "code"
CONF_METHOD = "method"
CONF_RECORD = "record"
CONF_PASSCODE = "passcode"
MAX_PASSCODE_ATTEMPTS = 3  # Apple locks escrow recovery after repeated failures

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))
CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): TextSelector(TextSelectorConfig(autocomplete="one-time-code"))})


def _login_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        }
    )


def _digits(code: str) -> str:
    return "".join(ch for ch in code if ch.isdigit())


class FindMyAppleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in once: iCloud devices (FMIP) and Find My network tags come from the same session."""

    VERSION = 1

    def __init__(self) -> None:
        self._input: dict[str, Any] = {}
        self._account: AsyncAppleAccount | None = None
        self._client: AsyncFindMyClient | None = None
        self._methods: list[Any] = []
        self._method: Any = None
        self._records: list[Any] = []
        self._passcode_attempts = 0
        self._devices: dict[str, TrackedItem] = {}
        self._accessories: list[FindMyAccessory] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: FindMyAppleConfigEntry) -> OptionsFlow:
        return FindMyAppleOptionsFlow()

    def async_remove(self) -> None:
        """The flow was aborted or finished: close any open Apple connections."""
        for closable in (self._client, self._account):
            if closable is not None:
                self.hass.async_create_task(closable.close())
        self._client = self._account = None

    # ============================== sign in ======================================================

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            self._input = {**user_input, CONF_USERNAME: username}
            result = await self._async_login(errors)
            if result is not None:
                return result
        return self.async_show_form(step_id="user", data_schema=_login_schema(user_input or {}), errors=errors)

    async def _async_login(self, errors: dict[str, str]) -> ConfigFlowResult | None:
        """Sign in as the account's emulated Mac (provisioning it on first use)."""
        from findmy import LoginState

        from .backends.findmy_network import MachineError, async_open_machine, classify, new_account

        assert self.unique_id is not None
        try:
            provider = await async_open_machine(machine_root(self.hass, self.unique_id), provision=True)
        except MachineError:
            _LOGGER.exception("Could not prepare the emulated Mac")
            errors["base"] = "machine_failed"
            return None
        if self._account is not None:
            await self._account.close()
        self._account = new_account(provider)
        try:
            state = await self._account.login(self._input[CONF_USERNAME], self._input[CONF_PASSWORD])
            if state == LoginState.REQUIRE_2FA:
                self._methods = list(await self._account.get_2fa_methods())
        except Exception as err:
            mapped = classify(err)
            _LOGGER.debug("Apple sign-in failed: %s", err)
            errors["base"] = {AuthFailed: "invalid_auth", MachineError: "machine_failed"}.get(
                type(mapped), "cannot_connect"
            )
            return None
        if state == LoginState.REQUIRE_2FA:
            if not self._methods:
                return self.async_abort(reason="security_key_unsupported")
            if len(self._methods) == 1:
                return await self._async_request_code(self._methods[0])
            return await self.async_step_2fa_method()
        return await self._async_after_login()

    async def async_step_2fa_method(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from .backends.findmy_network import two_factor_label

        if user_input is not None:
            return await self._async_request_code(self._methods[int(user_input[CONF_METHOD])])
        options = [SelectOptionDict(value=str(i), label=two_factor_label(m)) for i, m in enumerate(self._methods)]
        return self.async_show_form(
            step_id="2fa_method",
            data_schema=vol.Schema(
                {vol.Required(CONF_METHOD, default="0"): SelectSelector(SelectSelectorConfig(options=options))}
            ),
        )

    async def _async_request_code(self, method: Any) -> ConfigFlowResult:
        self._method = method
        try:
            await method.request()
        except Exception:
            _LOGGER.exception("Could not request a two-factor code")
            return self.async_abort(reason="cannot_connect")
        return await self.async_step_code()

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from findmy import LoginState

        from .backends.findmy_network import two_factor_label

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                state = await self._method.submit(_digits(user_input[CONF_CODE]))
            except Exception:
                _LOGGER.debug("Two-factor code refused", exc_info=True)
                state = None
            if state == LoginState.LOGGED_IN:
                return await self._async_after_login()
            errors["base"] = "invalid_code"
        return self.async_show_form(
            step_id="code",
            data_schema=CODE_SCHEMA,
            errors=errors,
            description_placeholders={"delivery": two_factor_label(self._method)},
        )

    # ============================== devices, then the keychain for tags ==========================

    async def _async_after_login(self) -> ConfigFlowResult:
        from findmy.icloud import AsyncFindMyClient

        from .backends.fmip import TOKEN_NAME, FmipClient
        from .macanisette import for_cloudkit

        assert self._account is not None
        tokens = self._account.service_tokens or {}
        if "searchPartyToken" not in tokens or TOKEN_NAME not in tokens:
            return self.async_abort(reason="no_findmy_token")
        if self.source == SOURCE_REAUTH:
            return await self._async_finish_reauth()

        fmip = FmipClient(self._account)
        try:
            self._devices = await fmip.fetch(locate=False)
        except NoDevices:
            self._devices = {}
        except (AuthFailed, Throttled):
            _LOGGER.exception("Could not list the account's devices")
            return self.async_abort(reason="cannot_connect")
        finally:
            await fmip.close()

        try:
            self._client = await AsyncFindMyClient.open(for_cloudkit(self._account))
            options = await self._client.recovery_options()
        except Exception:
            _LOGGER.exception("Could not open the iCloud keychain")
            return self.async_abort(reason="cannot_connect")
        self._records = list(options.recoverable)
        if not self._records:
            # No Apple device holds a keychain escrow record: devices work, tags can't be unlocked.
            return await self.async_step_select()
        return await self.async_step_unlock()

    async def async_step_unlock(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._client is not None
            record = self._records[int(user_input[CONF_RECORD])]
            passcode = user_input.pop(CONF_PASSCODE)
            self._passcode_attempts += 1
            try:
                await self._client.unlock(record, passcode)
                self._accessories = list(await self._client.accessories())
            except Exception as err:
                _LOGGER.debug("Keychain unlock failed: %s", err)
                if self._passcode_attempts >= MAX_PASSCODE_ATTEMPTS:
                    return self.async_abort(reason="too_many_passcode_attempts")
                errors["base"] = "invalid_passcode"
            else:
                return await self.async_step_select()
            finally:
                del passcode
        options = [SelectOptionDict(value=str(i), label=r.describe()) for i, r in enumerate(self._records)]
        return self.async_show_form(
            step_id="unlock",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_RECORD, default="0"): SelectSelector(SelectSelectorConfig(options=options)),
                    vol.Required(CONF_PASSCODE): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"attempts_left": str(MAX_PASSCODE_ATTEMPTS - self._passcode_attempts)},
        )

    async def async_step_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from .backends.findmy_network import describe, tag_id

        # The account's own Apple devices are in the keychain too; FMIP already lists them.
        tags = {tag_id(a): describe(a) for a in self._accessories if not describe(a).is_apple_device}
        labels = {item_id: _device_label(item) for item_id, item in self._devices.items()}
        labels.update({i: f"{t.name} ({t.model})" if t.model else t.name for i, t in tags.items()})
        if not labels:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            assert self._account is not None and self.unique_id is not None
            state = {
                "account": self._account.to_json(),
                "accessories": [a.to_json() for a in self._accessories],
            }
            await account_state_store(self.hass, self.unique_id).async_save(state)
            self.async_remove()  # close connections; the entry restores the session from the store
            return self.async_create_entry(
                title=self._input[CONF_USERNAME],
                data={CONF_USERNAME: self._input[CONF_USERNAME]},
                options={
                    CONF_TRACKED_ITEMS: user_input[CONF_TRACKED_ITEMS],
                    CONF_INCLUDE_FAMILY: user_input[CONF_INCLUDE_FAMILY],
                    CONF_TAG_INTERVAL: DEFAULT_TAG_INTERVAL,
                },
            )
        default = [i for i, d in self._devices.items() if d.owner is None] + list(tags)
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRACKED_ITEMS, default=default): _multi_select(labels),
                    vol.Required(CONF_INCLUDE_FAMILY, default=True): BooleanSelector(),
                }
            ),
        )

    # ============================== reauth =======================================================

    async def _async_finish_reauth(self) -> ConfigFlowResult:
        """Keep the tag keys, replace only the signed-in session."""
        assert self._account is not None
        entry = self._get_reauth_entry()
        store = account_state_store(self.hass, entry.unique_id or "")
        state = await store.async_load() or {}
        state["account"] = self._account.to_json()
        await store.async_save(state)
        self.async_remove()
        return self.async_update_reload_and_abort(entry)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._input = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            await self.async_set_unique_id(entry.unique_id)
            self._input = {**entry.data, **user_input}
            result = await self._async_login(errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            errors=errors,
            description_placeholders={"username": entry.data[CONF_USERNAME]},
        )


class FindMyAppleOptionsFlow(OptionsFlow):
    """Tracked devices and tags, family, and the polling intervals."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_AWAY_MIN_INTERVAL] > user_input[CONF_AWAY_MAX_INTERVAL]:
                errors[CONF_AWAY_MIN_INTERVAL] = "min_above_max"
            else:
                return self.async_create_entry(data=user_input)

        entry: FindMyAppleConfigEntry = self.config_entry
        options = entry.options
        known: dict[str, str] = {}
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            known.update(runtime.coordinator.all_devices)
        for item_id in options.get(CONF_TRACKED_ITEMS, []):
            known.setdefault(item_id, item_id)
        schema = vol.Schema(
            {
                vol.Required(CONF_TRACKED_ITEMS, default=options.get(CONF_TRACKED_ITEMS, list(known))): _multi_select(
                    known
                ),
                vol.Required(CONF_INCLUDE_FAMILY, default=options.get(CONF_INCLUDE_FAMILY, True)): BooleanSelector(),
                vol.Required(
                    CONF_HOME_INTERVAL, default=options.get(CONF_HOME_INTERVAL, DEFAULT_HOME_INTERVAL)
                ): _minutes(1, 120),
                vol.Required(
                    CONF_AWAY_MIN_INTERVAL, default=options.get(CONF_AWAY_MIN_INTERVAL, DEFAULT_AWAY_MIN_INTERVAL)
                ): _minutes(1, 120),
                vol.Required(
                    CONF_AWAY_MAX_INTERVAL, default=options.get(CONF_AWAY_MAX_INTERVAL, DEFAULT_AWAY_MAX_INTERVAL)
                ): _minutes(1, 120),
                vol.Required(
                    CONF_TAG_INTERVAL, default=options.get(CONF_TAG_INTERVAL, DEFAULT_TAG_INTERVAL)
                ): _minutes(5, 60),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)


def _device_label(device: TrackedItem) -> str:
    model = device.model_display_name or device.model
    label = f"{device.name} ({model})" if model else device.name
    return f"{label} – {device.owner}" if device.owner else label


def _multi_select(items: Mapping[str, str]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=k, label=v) for k, v in items.items()],
            multiple=True,
            mode=SelectSelectorMode.LIST,
        )
    )


def _minutes(low: int, high: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=low, max=high, step=1, unit_of_measurement="min", mode=NumberSelectorMode.BOX)
    )
