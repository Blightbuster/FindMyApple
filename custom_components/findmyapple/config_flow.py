"""Config flow: iCloud devices or Find My network tags, each with sign-in, two-factor, reauth and options."""

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

from . import FindMyAppleConfigEntry, machine_root, session_dir, tag_state_store
from .backends.icloud import (
    AuthFailed,
    DeviceSummary,
    ICloudBackend,
    NoDevices,
    NotActivated,
    Throttled,
    TwoFactorRequired,
)
from .const import (
    CONF_AWAY_MAX_INTERVAL,
    CONF_AWAY_MIN_INTERVAL,
    CONF_HOME_INTERVAL,
    CONF_INCLUDE_FAMILY,
    CONF_KIND,
    CONF_REGION,
    CONF_REMEMBER_PASSWORD,
    CONF_TAG_INTERVAL,
    CONF_TRACKED_DEVICES,
    CONF_TRACKED_TAGS,
    DEFAULT_AWAY_MAX_INTERVAL,
    DEFAULT_AWAY_MIN_INTERVAL,
    DEFAULT_HOME_INTERVAL,
    DEFAULT_TAG_INTERVAL,
    DOMAIN,
    KIND_ICLOUD,
    KIND_TAGS,
    REGION_CHINA,
    REGION_GLOBAL,
)

if TYPE_CHECKING:
    from findmy import AsyncAppleAccount, FindMyAccessory
    from findmy.icloud import AsyncFindMyClient

_LOGGER = logging.getLogger(__name__)

CONF_CODE = "code"
CONF_METHOD = "method"
CONF_RECORD = "record"
CONF_PASSCODE = "passcode"
MAX_PASSCODE_ATTEMPTS = 3  # Apple locks escrow recovery after repeated failures

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))
CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): TextSelector(TextSelectorConfig(autocomplete="one-time-code"))})


def _icloud_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
            vol.Required(CONF_REGION, default=defaults.get(CONF_REGION, REGION_GLOBAL)): SelectSelector(
                SelectSelectorConfig(options=[REGION_GLOBAL, REGION_CHINA], translation_key=CONF_REGION)
            ),
            vol.Required(CONF_REMEMBER_PASSWORD, default=defaults.get(CONF_REMEMBER_PASSWORD, True)): BooleanSelector(),
        }
    )


def _tags_schema(defaults: Mapping[str, Any]) -> vol.Schema:
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
    """Sign in to an Apple account for its iCloud devices or its Find My tags."""

    VERSION = 1

    def __init__(self) -> None:
        self._input: dict[str, Any] = {}
        self._backend: ICloudBackend | None = None
        self._devices: list[DeviceSummary] = []
        # tags
        self._account: AsyncAppleAccount | None = None
        self._client: AsyncFindMyClient | None = None
        self._methods: list[Any] = []
        self._method: Any = None
        self._records: list[Any] = []
        self._passcode_attempts = 0
        self._accessories: list[FindMyAccessory] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: FindMyAppleConfigEntry) -> OptionsFlow:
        return FindMyAppleOptionsFlow()

    def async_remove(self) -> None:
        """The flow was aborted or finished: close any open Find My connections."""
        for closable in (self._client, self._account):
            if closable is not None:
                self.hass.async_create_task(closable.close())
        self._client = self._account = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=[KIND_ICLOUD, KIND_TAGS])

    # ============================== iCloud devices ===============================================

    async def async_step_icloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            self._input = {**user_input, CONF_USERNAME: username}
            result = await self._async_icloud_login(errors)
            if result is not None:
                return result
        return self.async_show_form(step_id="icloud", data_schema=_icloud_schema(user_input or {}), errors=errors)

    async def _async_icloud_login(self, errors: dict[str, str]) -> ConfigFlowResult | None:
        """Sign in with self._input. Returns the next step, or None (with errors set) to re-show the form."""
        assert self.unique_id is not None
        self._backend = ICloudBackend(
            self._input[CONF_USERNAME],
            self._input[CONF_PASSWORD],
            session_dir(self.hass, self.unique_id),
            china=self._input.get(CONF_REGION) == REGION_CHINA,
        )
        try:
            await self.hass.async_add_executor_job(self._backend.login)
        except TwoFactorRequired:
            if self._backend.uses_security_key:
                return self.async_abort(reason="security_key_unsupported")
            return await self.async_step_two_factor()
        except AuthFailed:
            errors["base"] = "invalid_auth"
            return None
        except Throttled:
            errors["base"] = "cannot_connect"
            return None
        except Exception:
            _LOGGER.exception("Unexpected error signing in to iCloud")
            errors["base"] = "unknown"
            return None
        return await self._async_icloud_after_login()

    async def async_step_two_factor(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._backend is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                ok = await self.hass.async_add_executor_job(self._backend.validate_2fa_code, _digits(user_input[CONF_CODE]))
            except Exception:
                _LOGGER.exception("Unexpected error validating the two-factor code")
                ok = False
            if ok:
                return await self._async_icloud_after_login()
            errors["base"] = "invalid_code"
        return self.async_show_form(
            step_id="two_factor",
            data_schema=CODE_SCHEMA,
            errors=errors,
            description_placeholders={"delivery": self._backend.two_factor_delivery},
        )

    async def _async_icloud_after_login(self) -> ConfigFlowResult:
        if self.source == SOURCE_REAUTH:
            return self._async_finish_icloud_reauth()
        assert self._backend is not None
        try:
            self._devices = await self.hass.async_add_executor_job(self._backend.list_devices)
        except NotActivated:
            return self.async_abort(reason="not_activated")
        except NoDevices:
            return self.async_abort(reason="no_devices")
        except (Throttled, AuthFailed, TwoFactorRequired):
            return self.async_abort(reason="cannot_connect")
        finally:
            await self.hass.async_add_executor_job(self._backend.close)
        return await self.async_step_select_devices()

    async def async_step_select_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = {
                CONF_KIND: KIND_ICLOUD,
                CONF_USERNAME: self._input[CONF_USERNAME],
                CONF_REGION: self._input[CONF_REGION],
            }
            if self._input.get(CONF_REMEMBER_PASSWORD, True):
                data[CONF_PASSWORD] = self._input[CONF_PASSWORD]
            return self.async_create_entry(
                title=self._input[CONF_USERNAME],
                data=data,
                options={
                    CONF_TRACKED_DEVICES: user_input[CONF_TRACKED_DEVICES],
                    CONF_INCLUDE_FAMILY: user_input[CONF_INCLUDE_FAMILY],
                },
            )
        return self.async_show_form(
            step_id="select_devices",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TRACKED_DEVICES, default=[d.id for d in self._devices if d.owner is None]
                    ): _multi_select({d.id: _device_label(d) for d in self._devices}),
                    vol.Required(CONF_INCLUDE_FAMILY, default=True): BooleanSelector(),
                }
            ),
        )

    def _async_finish_icloud_reauth(self) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        data = {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
        if self._input.get(CONF_REMEMBER_PASSWORD, True):
            data[CONF_PASSWORD] = self._input[CONF_PASSWORD]
        if self._backend is not None:
            self._backend.close()
        return self.async_update_reload_and_abort(entry, data=data)

    # ============================== Find My network tags =========================================

    async def async_step_tags(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(f"{username.lower()}#tags")
            self._abort_if_unique_id_configured()
            self._input = {**user_input, CONF_USERNAME: username}
            result = await self._async_tags_login(errors)
            if result is not None:
                return result
        return self.async_show_form(step_id="tags", data_schema=_tags_schema(user_input or {}), errors=errors)

    async def _async_tags_login(self, errors: dict[str, str]) -> ConfigFlowResult | None:
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
            _LOGGER.debug("Find My sign-in failed: %s", err)
            errors["base"] = {AuthFailed: "invalid_auth", MachineError: "machine_failed"}.get(
                type(mapped), "cannot_connect"
            )
            return None
        if state == LoginState.REQUIRE_2FA:
            if not self._methods:
                return self.async_abort(reason="security_key_unsupported")
            if len(self._methods) == 1:
                return await self._async_tags_request_code(self._methods[0])
            return await self.async_step_tags_2fa_method()
        return await self._async_tags_after_login()

    async def async_step_tags_2fa_method(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from .backends.findmy_network import two_factor_label

        if user_input is not None:
            return await self._async_tags_request_code(self._methods[int(user_input[CONF_METHOD])])
        options = [SelectOptionDict(value=str(i), label=two_factor_label(m)) for i, m in enumerate(self._methods)]
        return self.async_show_form(
            step_id="tags_2fa_method",
            data_schema=vol.Schema(
                {vol.Required(CONF_METHOD, default="0"): SelectSelector(SelectSelectorConfig(options=options))}
            ),
        )

    async def _async_tags_request_code(self, method: Any) -> ConfigFlowResult:
        self._method = method
        try:
            await method.request()
        except Exception:
            _LOGGER.exception("Could not request a two-factor code")
            return self.async_abort(reason="cannot_connect")
        return await self.async_step_tags_code()

    async def async_step_tags_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
                return await self._async_tags_after_login()
            errors["base"] = "invalid_code"
        return self.async_show_form(
            step_id="tags_code",
            data_schema=CODE_SCHEMA,
            errors=errors,
            description_placeholders={"delivery": two_factor_label(self._method)},
        )

    async def _async_tags_after_login(self) -> ConfigFlowResult:
        from findmy.icloud import AsyncFindMyClient

        from .macanisette import for_cloudkit

        assert self._account is not None
        if "searchPartyToken" not in (self._account.service_tokens or {}):
            return self.async_abort(reason="no_findmy_token")
        if self.source == SOURCE_REAUTH:
            return await self._async_finish_tags_reauth()
        try:
            self._client = await AsyncFindMyClient.open(for_cloudkit(self._account))
            options = await self._client.recovery_options()
        except Exception:
            _LOGGER.exception("Could not open the iCloud keychain")
            return self.async_abort(reason="cannot_connect")
        self._records = list(options.recoverable)
        if not self._records:
            return self.async_abort(reason="no_escrow")
        return await self.async_step_tags_unlock()

    async def async_step_tags_unlock(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
                return await self.async_step_tags_select()
            finally:
                del passcode
        options = [SelectOptionDict(value=str(i), label=r.describe()) for i, r in enumerate(self._records)]
        return self.async_show_form(
            step_id="tags_unlock",
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

    async def async_step_tags_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from .backends.findmy_network import describe

        infos = [describe(a) for a in self._accessories]
        if not infos:
            return self.async_abort(reason="no_tags")
        if user_input is not None:
            assert self._account is not None and self.unique_id is not None
            state = {
                "account": self._account.to_json(),
                "accessories": [a.to_json() for a in self._accessories],
            }
            await tag_state_store(self.hass, self.unique_id).async_save(state)
            self.async_remove()  # close connections; the entry restores the session from the store
            return self.async_create_entry(
                title=f"{self._input[CONF_USERNAME]} (tags)",
                data={CONF_KIND: KIND_TAGS, CONF_USERNAME: self._input[CONF_USERNAME]},
                options={CONF_TRACKED_TAGS: user_input[CONF_TRACKED_TAGS], CONF_TAG_INTERVAL: DEFAULT_TAG_INTERVAL},
            )
        labels = {i.id: f"{i.name} ({i.model})" if i.model else i.name for i in infos}
        return self.async_show_form(
            step_id="tags_select",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TRACKED_TAGS, default=[i.id for i in infos if not i.is_apple_device]
                    ): _multi_select(labels),
                }
            ),
        )

    async def _async_finish_tags_reauth(self) -> ConfigFlowResult:
        """Keep the tag keys, replace only the signed-in session."""
        assert self._account is not None
        entry = self._get_reauth_entry()
        store = tag_state_store(self.hass, entry.unique_id or "")
        state = await store.async_load() or {}
        state["account"] = self._account.to_json()
        await store.async_save(state)
        self.async_remove()
        return self.async_update_reload_and_abort(entry)

    # ============================== reauth (both kinds) ==========================================

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._input = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        is_tags = entry.data.get(CONF_KIND) == KIND_TAGS
        if user_input is not None:
            await self.async_set_unique_id(entry.unique_id)
            self._input = {**entry.data, **user_input}
            result = (
                await self._async_tags_login(errors) if is_tags else await self._async_icloud_login(errors)
            )
            if result is not None:
                return result
        schema: dict[Any, Any] = {vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}
        if not is_tags:
            schema[vol.Required(CONF_REMEMBER_PASSWORD, default=CONF_PASSWORD in entry.data)] = BooleanSelector()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"username": entry.data[CONF_USERNAME]},
        )


class FindMyAppleOptionsFlow(OptionsFlow):
    """iCloud: devices, family, intervals. Tags: which tags, poll interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry: FindMyAppleConfigEntry = self.config_entry
        if entry.data.get(CONF_KIND) == KIND_TAGS:
            return await self.async_step_tags(user_input)
        return await self.async_step_icloud(user_input)

    def _known(self, option: str) -> dict[str, str]:
        entry: FindMyAppleConfigEntry = self.config_entry
        known: dict[str, str] = {}
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            known.update(runtime.coordinator.all_devices)
        for item_id in entry.options.get(option, []):
            known.setdefault(item_id, item_id)
        return known

    async def async_step_icloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_AWAY_MIN_INTERVAL] > user_input[CONF_AWAY_MAX_INTERVAL]:
                errors[CONF_AWAY_MIN_INTERVAL] = "min_above_max"
            else:
                return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        known = self._known(CONF_TRACKED_DEVICES)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TRACKED_DEVICES, default=options.get(CONF_TRACKED_DEVICES, list(known))
                ): _multi_select(known),
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
            }
        )
        return self.async_show_form(step_id="icloud", data_schema=schema, errors=errors)

    async def async_step_tags(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        known = self._known(CONF_TRACKED_TAGS)
        schema = vol.Schema(
            {
                vol.Required(CONF_TRACKED_TAGS, default=options.get(CONF_TRACKED_TAGS, list(known))): _multi_select(
                    known
                ),
                vol.Required(
                    CONF_TAG_INTERVAL, default=options.get(CONF_TAG_INTERVAL, DEFAULT_TAG_INTERVAL)
                ): _minutes(5, 60),
            }
        )
        return self.async_show_form(step_id="tags", data_schema=schema)


def _device_label(device: DeviceSummary) -> str:
    label = f"{device.name} ({device.model})" if device.model else device.name
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
