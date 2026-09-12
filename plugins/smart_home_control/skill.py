"""Smart-home device control via Home Assistant — reuses the same HA
instance/token clockmost-console already talks to (its CM_HA_URL/
CM_HA_TOKEN, confirmed live 2026-09-12), not a new integration. Device
names are never hardcoded here: they're read live from HA's own
`friendly_name` attribute for whichever entities are explicitly allowed
(FELT_HA_ENTITIES), so this works for whatever's actually configured
rather than baking in Mario's specific devices.

Deliberately an explicit allowlist, not "control anything HA knows
about" — the entities available (lights, a fan, two smart plugs) are all
low-stakes, but the pattern itself (blind voice control over an entire
home-automation instance) is the kind of thing that should be opted into
per-entity, not a default a new deployment gets for free.
"""

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "smart_home_control"
description = (
    "Turns a smart-home device (light, plug, fan) on or off, or checks whether it's "
    "currently on — by name, e.g. 'turn on the chandelier', 'is the fan on'."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "device": {"type": "string", "description": "The device's name, as Mario would say it"},
        "action": {"type": "string", "description": "'on', 'off', or 'status' (default if omitted)"},
    },
    "required": ["device"],
}


async def _fetch_allowed_states(client: httpx.AsyncClient, config) -> dict[str, dict]:
    """entity_id -> {"friendly_name": ..., "state": ...} for just the
    allowlisted entities — one call per entity rather than fetching HA's
    entire state list and filtering, so a large HA instance elsewhere
    doesn't cost more here than the handful of entities actually allowed."""
    states = {}
    for entity_id in config.ha_entities_list():
        response = await client.get(f"{config.ha_url}/api/states/{entity_id}")
        if response.status_code == 200:
            data = response.json()
            states[entity_id] = {
                "friendly_name": data.get("attributes", {}).get("friendly_name", entity_id),
                "state": data.get("state", "unknown"),
            }
    return states


def _match(query: str, states: dict[str, dict]) -> list[str]:
    """Same exact-wins-else-substring spirit as get_container_status's
    matching_containers, but bidirectional — a real 2026-09-12 finding:
    the router doesn't reliably send just the device's own name. It
    sometimes drops it for a generic category word instead ("the shelf
    plug" -> "plug", which IS a substring of "ExtraPlug" so a one-way
    check still works there by luck), and sometimes keeps a generic word
    alongside the real name ("shelf plug" for a device actually just
    named "Shelf" — the real name is a substring of THAT query, not the
    other way around, so a query-in-name-only check misses it). Checking
    both directions covers either mistake without needing the router to
    guess exactly right."""
    query = query.lower()
    exact = [eid for eid, info in states.items() if info["friendly_name"].lower() == query]
    if exact:
        return exact
    return [
        eid
        for eid, info in states.items()
        if query in info["friendly_name"].lower() or info["friendly_name"].lower() in query
    ]


async def handle(params, ctx):
    config = ctx.config
    if not config.ha_url or not config.ha_token or not config.ha_entities_list():
        return SkillResult(text="Smart home control isn't configured (no Home Assistant entities allowed).")

    device = get_single_param(params, "device", "name", "entity", ignore_keys=("action", "command"))
    if not device:
        return SkillResult(text="Which device?")

    headers = {"Authorization": f"Bearer {config.ha_token}"}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        try:
            states = await _fetch_allowed_states(client, config)
        except httpx.HTTPError:
            return SkillResult(text="Couldn't reach Home Assistant right now.")

        matches = _match(str(device), states)
        if not matches:
            return SkillResult(text=f"I don't see a device matching '{device}'.")
        if len(matches) > 1:
            names = ", ".join(states[eid]["friendly_name"] for eid in matches)
            return SkillResult(text=f"Multiple devices match '{device}': {names}. Which one?")

        entity_id = matches[0]
        friendly_name = states[entity_id]["friendly_name"]
        action = str(
            get_single_param(params, "action", "command", ignore_keys=("device", "name", "entity")) or "status"
        ).lower()

        if "on" in action and "off" not in action:
            service = "turn_on"
        elif "off" in action:
            service = "turn_off"
        else:
            is_on = states[entity_id]["state"] == "on"
            return SkillResult(text=f"{friendly_name} is currently {'on' if is_on else 'off'}.")

        domain = entity_id.split(".")[0]
        try:
            response = await client.post(
                f"{config.ha_url}/api/services/{domain}/{service}", json={"entity_id": entity_id}
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return SkillResult(text=f"Couldn't reach Home Assistant to control {friendly_name}.")

    return SkillResult(text=f"{friendly_name} turned {'on' if service == 'turn_on' else 'off'}.")
