"""The Felt panel: the Felt app's Talk and Margins inside Home Assistant's
sidebar (felt-ha-hub, 2026-10-19) - so the HA app on the phone is all Mario
needs. Controls, lists and systems are native HA already.

The page (felt-dashboard's HA build, in panel/) never holds a Felt
credential: it asks this integration over HA's own websocket, and the
integration calls felt-core's /v1/ha/app/* with its ha-felt token. Admin
only, and only the app routes the page uses.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .client import FeltClient, FeltError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_PATH = "felt"
STATIC_URL = "/felt_panel"
_PANEL_DIR = Path(__file__).parent / "panel"

# exactly the calls the page makes (felt-dashboard src/lib/appApi.ts)
_ROUTES = frozenset(
    {
        ("GET", "/me"),
        ("GET", "/history"),
        ("GET", "/asides"),
        ("GET", "/margins"),
        ("GET", "/margins/count"),
        ("POST", "/margins/mark"),
        ("POST", "/marks/seen"),
        ("POST", "/chat"),
    }
)


def _client(hass: HomeAssistant) -> FeltClient | None:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data.client
    return None


async def async_setup(hass: HomeAssistant, version: str) -> None:
    """Once per HA run: the page's files and the two websocket commands."""
    if hass.data.get(f"{DOMAIN}_panel_setup"):
        return
    hass.data[f"{DOMAIN}_panel_setup"] = True
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(_PANEL_DIR), cache_headers=False)]
    )
    websocket_api.async_register_command(hass, ws_call)
    websocket_api.async_register_command(hass, ws_stream)
    hass.data[f"{DOMAIN}_panel_version"] = version


async def async_register(hass: HomeAssistant, version: str) -> None:
    await async_setup(hass, version)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=URL_PATH,
        webcomponent_name="felt-panel",
        sidebar_title="Felt",
        sidebar_icon="mdi:notebook-heart-outline",
        module_url=f"{STATIC_URL}/felt-panel.js?v={version}",
        # its own document: the app's CSS, fonts and #hash routing stay as they are
        embed_iframe=True,
        require_admin=True,
    )


@callback
def async_unregister(hass: HomeAssistant) -> None:
    frontend.async_remove_panel(hass, URL_PATH)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "felt/app/call",
        vol.Required("method"): vol.In(["GET", "POST"]),
        vol.Required("path"): str,
        vol.Optional("body"): dict,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_call(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    path: str = msg["path"]
    if (msg["method"], path.split("?", 1)[0]) not in _ROUTES:
        connection.send_error(msg["id"], "not_allowed", "not a Felt panel route")
        return
    client = _client(hass)
    if client is None:
        connection.send_error(msg["id"], "not_loaded", "the Felt integration isn't set up")
        return
    try:
        result = await client.app_call(msg["method"], path, msg.get("body"))
    except FeltError as e:
        connection.send_error(msg["id"], "felt_unavailable", str(e))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "felt/app/stream"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_stream(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """felt-core's live notices, relayed as subscription events. Reconnects
    after a break; felt-core's "hello" on each connect makes the page refetch."""
    client = _client(hass)
    if client is None:
        connection.send_error(msg["id"], "not_loaded", "the Felt integration isn't set up")
        return

    async def relay() -> None:
        delay = 2.0
        while True:
            try:
                async for event, data in client.app_stream():
                    connection.send_message(
                        websocket_api.event_message(msg["id"], {"event": event, "data": data})
                    )
                    delay = 2.0
            except FeltError as e:
                _LOGGER.debug("Felt panel stream: %s", e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    task = hass.async_create_background_task(relay(), "felt panel stream")
    connection.subscriptions[msg["id"]] = task.cancel
    connection.send_result(msg["id"])
