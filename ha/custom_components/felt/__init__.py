"""Felt as Home Assistant's conversation agent."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import FeltClient
from .const import CONF_CLIENT_ID, CONF_PASSWORD, CONF_TOKEN_URL, CONF_URL, CONF_USERNAME
from .coordinator import FeltCoordinator

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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
