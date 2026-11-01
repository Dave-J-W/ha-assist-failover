"""Choose the primary and backup engines. Either pair may be left out."""

from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CONF_PRIMARY_TIMEOUT,
    CONF_STT_BACKUP,
    CONF_STT_PRIMARY,
    CONF_TTS_BACKUP,
    CONF_TTS_PRIMARY,
    DEFAULT_PRIMARY_TIMEOUT,
    DOMAIN,
)

PAIRS = ((CONF_STT_PRIMARY, CONF_STT_BACKUP), (CONF_TTS_PRIMARY, CONF_TTS_BACKUP))


def _schema(values):
    def engine(key, domain):
        field = vol.Optional(key, description={"suggested_value": values.get(key)})
        return field, selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))

    return vol.Schema(
        dict(
            [
                engine(CONF_STT_PRIMARY, "stt"),
                engine(CONF_STT_BACKUP, "stt"),
                engine(CONF_TTS_PRIMARY, "tts"),
                engine(CONF_TTS_BACKUP, "tts"),
                (
                    vol.Required(
                        CONF_PRIMARY_TIMEOUT,
                        default=values.get(CONF_PRIMARY_TIMEOUT, DEFAULT_PRIMARY_TIMEOUT),
                    ),
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=60,
                            step=1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                ),
            ]
        )
    )


def _validate(values):
    errors = {}
    for primary, backup in PAIRS:
        first, second = values.get(primary), values.get(backup)
        if bool(first) != bool(second):
            errors[backup if first else primary] = "need_both"
        elif first and first == second:
            errors[backup] = "same_engine"
    if not errors and not any(values.get(primary) for primary, _ in PAIRS):
        errors["base"] = "nothing_chosen"
    return errors


class AssistFailoverConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors = _validate(user_input) if user_input is not None else {}
        if user_input is not None and not errors:
            return self.async_create_entry(title="Assist Failover", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AssistFailoverOptionsFlow()


class AssistFailoverOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = _validate(user_input) if user_input is not None else {}
        if user_input is not None and not errors:
            return self.async_create_entry(data=user_input)
        values = user_input or {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(values), errors=errors)
