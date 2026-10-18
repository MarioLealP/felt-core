"""Set up Felt: where felt-core is, and the ha-felt login (Authentik)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import FeltClient, FeltError
from .const import (
    CONF_CLIENT_ID,
    CONF_PASSWORD,
    CONF_TOKEN_URL,
    CONF_URL,
    CONF_USERNAME,
    DEFAULT_CLIENT_ID,
    DEFAULT_TOKEN_URL,
    DEFAULT_URL,
    DEFAULT_USERNAME,
    DOMAIN,
)

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, default=DEFAULT_URL): str,
        vol.Required(CONF_TOKEN_URL, default=DEFAULT_TOKEN_URL): str,
        vol.Required(CONF_CLIENT_ID, default=DEFAULT_CLIENT_ID): str,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class FeltConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            client = FeltClient(
                async_get_clientsession(self.hass),
                user_input[CONF_URL],
                user_input[CONF_TOKEN_URL],
                user_input[CONF_CLIENT_ID],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                await client.check()
            except FeltError:
                errors["base"] = "cannot_login"
            else:
                return self.async_create_entry(title="Felt", data=user_input)
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)
