"""The `felt.tell` action: an automation hands Felt plain text, she words it
and says it at the desk + on the phone (felt-core POST /v1/ha/tell,
ha_tell.py). Optional response: {"said": her line, or None when her ha.tell
feature is off}.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .client import FeltClient, FeltError
from .const import DOMAIN

SERVICE = "tell"

SCHEMA = vol.Schema(
    {
        vol.Required("text"): vol.All(cv.string, vol.Length(min=1, max=1000)),
        vol.Optional("title", default="Felt"): vol.All(cv.string, vol.Length(min=1, max=100)),
        vol.Optional("tag", default="ha"): vol.Match(r"^[a-z0-9-]{1,40}$"),
    }
)


def async_register(hass: HomeAssistant, client: FeltClient) -> None:
    async def handle(call: ServiceCall) -> ServiceResponse:
        try:
            return await client.tell(call.data["text"], call.data["title"], call.data["tag"])
        except FeltError as e:
            raise HomeAssistantError(f"Felt couldn't take it: {e}") from e

    hass.services.async_register(DOMAIN, SERVICE, handle, schema=SCHEMA, supports_response=SupportsResponse.OPTIONAL)


def async_unregister(hass: HomeAssistant) -> None:
    hass.services.async_remove(DOMAIN, SERVICE)
