"""Config & options flow for HA Audit."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ANALYZE_DB, CONF_INCLUDE_DIAGNOSTIC, CONF_INTERVAL_DAYS, CONF_NOTIFY,
    CONF_STALE_DAYS, CONF_UNAVAIL_DAYS, DEFAULTS, DOMAIN,
)


def _number(lo: int, hi: int, unit: str) -> selector.NumberSelector:
    return selector.NumberSelector(selector.NumberSelectorConfig(
        min=lo, max=hi, step=1, mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement=unit))


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_INTERVAL_DAYS, default=defaults[CONF_INTERVAL_DAYS]): _number(0, 90, "d"),
        vol.Required(CONF_STALE_DAYS, default=defaults[CONF_STALE_DAYS]): _number(1, 365, "d"),
        vol.Required(CONF_UNAVAIL_DAYS, default=defaults[CONF_UNAVAIL_DAYS]): _number(0, 30, "d"),
        vol.Required(CONF_ANALYZE_DB, default=defaults[CONF_ANALYZE_DB]): bool,
        vol.Required(CONF_INCLUDE_DIAGNOSTIC, default=defaults[CONF_INCLUDE_DIAGNOSTIC]): bool,
        vol.Required(CONF_NOTIFY, default=defaults[CONF_NOTIFY]): bool,
    })


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    for k in (CONF_INTERVAL_DAYS, CONF_STALE_DAYS, CONF_UNAVAIL_DAYS):
        out[k] = int(out[k])
    return out


class HaAuditConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="HA Audit", data={}, options=_clean(user_input))
        return self.async_show_form(step_id="user", data_schema=_schema(DEFAULTS))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return HaAuditOptionsFlow()


class HaAuditOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_clean(user_input))
        current = {**DEFAULTS, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
