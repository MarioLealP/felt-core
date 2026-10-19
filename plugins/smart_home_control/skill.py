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

Dimming (2026-10-19, Mario: "a dim the lights call without giving a %"):
`dim` sets the lights that can dim to a percentage - the one he said, or
FELT_HA_DIM_PERCENT. Plain commands usually reach Home Assistant's own
matcher first (ha_assist.py); this is the model's path for what it misses
("dim the chandelure", a typo'd "30 percente").
"""

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "smart_home_control"
description = (
    "Turns a light, plug or fan on or off, dims lights (to a % or the usual level), or "
    "checks if it's on, e.g. 'turn off the lights' (plural = all of that kind), 'dim the "
    "chandelure to 30%'."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "device": {"type": "string", "description": "Its name, as Mario says it"},
        "action": {
            "type": "string",
            "description": "Default status",
            "enum": ["on", "off", "dim", "status"],
        },
        "brightness": {
            "type": "integer",
            "description": "Percent 1-100, only with dim, only if Mario says a number",
        },
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
            attributes = data.get("attributes", {})
            modes = attributes.get("supported_color_modes") or []
            states[entity_id] = {
                "friendly_name": attributes.get("friendly_name", entity_id),
                "state": data.get("state", "unknown"),
                # a light that can be dimmed (not just switched)
                "dimmable": any(mode != "onoff" for mode in modes),
            }
    return states


# what Mario calls a kind of device -> its Home Assistant domain
_KINDS = {
    "fan": "fan",
    "fans": "fan",
    "light": "light",
    "lights": "light",
    "lamp": "light",
    "lamps": "light",
    "plug": "switch",
    "plugs": "switch",
    "switch": "switch",
    "switches": "switch",
}
_PLURALS = {"fans", "lights", "lamps", "plugs", "switches"}
_FILLER = {"the", "my", "all", "a"}


def _norm(text: str) -> str:
    """Lowercase letters and digits only: "front left" and "FrontLeft" compare equal."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _match(query: str, states: dict[str, dict]) -> tuple[list[str], bool]:
    """The entities `query` means, and whether it means all of them at once.

    By name first: exact (ignoring case and spaces) wins, else a substring
    either way - a real 2026-09-12 finding: the router sometimes sends a
    generic word instead of the name ("plug" for "ExtraPlug") and sometimes
    keeps one beside it ("shelf plug" for "Shelf"). Then by kind (2026-10-17:
    HA's names are things like "Core 300S Series" and "FrontLeft", so "the
    fan" found nothing): a kind word picks that domain's entities, and a
    plural or "all" ("the lights", "all plugs") means every one of them."""
    words = [w for w in query.lower().split() if w not in _FILLER]
    group = "all" in query.lower().split() or any(w in _PLURALS for w in words)
    q = _norm(" ".join(words))
    names = {eid: _norm(info["friendly_name"]) for eid, info in states.items()}
    exact = [eid for eid, name in names.items() if name == q]
    if exact:
        return exact, False
    if q and not (len(words) == 1 and words[0] in _KINDS):
        by_name = [eid for eid, name in names.items() if q in name or (name and name in q)]
        if by_name:
            return by_name, False
    kinds = {_KINDS[w] for w in words if w in _KINDS}
    if len(kinds) == 1:
        domain = kinds.pop()
        return [eid for eid in states if eid.split(".")[0] == domain], group
    return [], False


def dim_percent(params, config) -> int:
    """Pure: the brightness he said (1-100), else the usual dim level."""
    try:
        said = int(params.get("brightness"))
    except (TypeError, ValueError):
        return config.ha_dim_percent
    return min(100, max(1, said))


async def _dim(client, config, params, states, matches, label) -> SkillResult:
    dimmable = [eid for eid in matches if states[eid].get("dimmable")]
    if not dimmable:
        return SkillResult(text=f"{label} can't be dimmed - only switched on or off.")
    percent = dim_percent(params, config)
    try:
        response = await client.post(
            f"{config.ha_url}/api/services/light/turn_on",
            json={"entity_id": dimmable, "brightness_pct": percent},
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return SkillResult(text=f"Couldn't reach Home Assistant to dim {label}.")
    if len(dimmable) == 1:
        return SkillResult(text=f"{states[dimmable[0]]['friendly_name']} dimmed to {percent}%.")
    skipped = [states[eid]["friendly_name"] for eid in matches if eid not in dimmable]
    rest = f" ({', '.join(skipped)} can't dim)" if skipped else ""
    return SkillResult(text=f"{len(dimmable)} lights dimmed to {percent}%{rest}.")


async def handle(params, ctx):
    config = ctx.config
    if not config.ha_url or not config.ha_token or not config.ha_entities_list():
        return SkillResult(
            text="Smart home control isn't configured (no Home Assistant entities allowed)."
        )

    device = get_single_param(params, "device", "name", "entity", ignore_keys=("action", "command"))
    if not device:
        return SkillResult(text="Which device?")

    headers = {"Authorization": f"Bearer {config.ha_token}"}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        try:
            states = await _fetch_allowed_states(client, config)
        except httpx.HTTPError:
            return SkillResult(text="Couldn't reach Home Assistant right now.")

        matches, group = _match(str(device), states)
        if not matches:
            return SkillResult(text=f"I don't see a device matching '{device}'.")
        if len(matches) > 1 and not group:
            names = ", ".join(states[eid]["friendly_name"] for eid in matches)
            return SkillResult(text=f"Multiple devices match '{device}': {names}. Which one?")

        label = states[matches[0]]["friendly_name"] if len(matches) == 1 else f"all {len(matches)}"
        action = str(
            get_single_param(params, "action", "command", ignore_keys=("device", "name", "entity"))
            or "status"
        ).lower()

        if action == "dim" or params.get("brightness") is not None:
            return await _dim(client, config, params, states, matches, label)
        if "on" in action and "off" not in action:
            service = "turn_on"
        elif "off" in action:
            service = "turn_off"
        else:
            on = [states[eid]["friendly_name"] for eid in matches if states[eid]["state"] == "on"]
            if len(matches) == 1:
                return SkillResult(text=f"{label} is currently {'on' if on else 'off'}.")
            if not on:
                return SkillResult(text=f"All {len(matches)} are off.")
            if len(on) == len(matches):
                return SkillResult(text=f"All {len(matches)} are on.")
            return SkillResult(text=f"{len(on)} of {len(matches)} are on: {', '.join(on)}.")

        # one call per domain (a group is always one domain; HA takes a list of entities)
        domain = matches[0].split(".")[0]
        try:
            response = await client.post(
                f"{config.ha_url}/api/services/{domain}/{service}", json={"entity_id": matches}
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return SkillResult(text=f"Couldn't reach Home Assistant to control {label}.")

    return SkillResult(text=f"{label} turned {'on' if service == 'turn_on' else 'off'}.")
