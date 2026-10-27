"""Felt as Home Assistant's conversation agent."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import mail
from .client import FeltClient, FeltError
from .const import CONF_CLIENT_ID, CONF_PASSWORD, CONF_TOKEN_URL, CONF_URL, CONF_USERNAME
from .coordinator import FeltCoordinator
from .panel import async_register as register_panel
from .panel import async_unregister as unregister_panel

# bumped with manifest.json; also busts the browser cache of the panel
VERSION = "0.6.0"

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CONVERSATION, Platform.SENSOR, Platform.SWITCH]


@dataclass
class FeltData:
    client: FeltClient
    coordinator: FeltCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    d = entry.data
    client = FeltClient(
        async_get_clientsession(hass),
        d[CONF_URL],
        d[CONF_TOKEN_URL],
        d[CONF_CLIENT_ID],
        d[CONF_USERNAME],
        d[CONF_PASSWORD],
    )
    coordinator = FeltCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = FeltData(client, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await register_panel(hass, VERSION)

    async def forward(body: dict) -> None:
        try:
            await client.email(body)
        except FeltError as e:
            _LOGGER.warning("Felt didn't take a new mail: %s", e)

    @callback
    def on_mail(event: Event) -> None:
        body = mail.payload(event.data)
        if body is not None:
            entry.async_create_background_task(hass, forward(body), "felt-email")

    entry.async_on_unload(hass.bus.async_listen(mail.EVENT, on_mail))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unregister_panel(hass)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
