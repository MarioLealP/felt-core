"""The shopping list lives in Home Assistant (felt-ha-hub step 3, 2026-10-18):
its `todo.shopping_list`, the one the HA app's Home dashboard shows. Before
this Felt kept her own table, so there were two lists. Now the skill, the
Felt app's Lists page and the old /shopping-list phone page all read and
write HA's, through these few calls.

An item is {"id": HA's uid, "text": its summary, "checked": completed?},
the same shape the app had from the old table, ids now strings.
"""

import httpx

from felt_core.config import FeltConfig


class ShoppingUnavailable(Exception):
    """HA isn't configured or didn't answer."""


async def _call(config: FeltConfig, service: str, data: dict | None = None) -> dict:
    if not (config.ha_url and config.ha_token and config.ha_shopping_list):
        raise ShoppingUnavailable("Home Assistant isn't configured")
    url = f"{config.ha_url}/api/services/todo/{service}"
    if service == "get_items":
        url += "?return_response"
    body = {"entity_id": config.ha_shopping_list, **(data or {})}
    headers = {"Authorization": f"Bearer {config.ha_token}"}
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            response = await client.post(url, json=body)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        raise ShoppingUnavailable(str(e)) from e


async def items(config: FeltConfig) -> list[dict]:
    """Every item, unchecked first, otherwise in HA's order."""
    data = await _call(config, "get_items")
    raw = data.get("service_response", {}).get(config.ha_shopping_list, {}).get("items", [])
    found = [
        {"id": i["uid"], "text": i["summary"], "checked": i.get("status") == "completed"}
        for i in raw
        if i.get("uid") and i.get("summary")
    ]
    return sorted(found, key=lambda i: i["checked"])


async def add(config: FeltConfig, text: str) -> None:
    await _call(config, "add_item", {"item": text})


async def set_checked(config: FeltConfig, item_id: str, checked: bool) -> None:
    status = "completed" if checked else "needs_action"
    await _call(config, "update_item", {"item": item_id, "status": status})


async def remove(config: FeltConfig, item_id: str) -> None:
    await _call(config, "remove_item", {"item": item_id})


async def clear_checked(config: FeltConfig) -> None:
    await _call(config, "remove_completed_items")


async def version(config: FeltConfig) -> str:
    """Changes when the list does (the entity's state is the unchecked count),
    for the app's live "lists changed" notice. Empty when HA can't be asked."""
    if not (config.ha_url and config.ha_token and config.ha_shopping_list):
        return ""
    headers = {"Authorization": f"Bearer {config.ha_token}"}
    try:
        async with httpx.AsyncClient(timeout=5, headers=headers) as client:
            response = await client.get(f"{config.ha_url}/api/states/{config.ha_shopping_list}")
            response.raise_for_status()
            state = response.json()
    except httpx.HTTPError:
        return ""
    return f"{state.get('state')}|{state.get('last_updated')}"
