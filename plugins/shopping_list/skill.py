"""A running shopping list - Home Assistant's todo.shopping_list (shopping.py),
the same list the HA app and the Felt app's Lists page check items off.
Distinct from remember_fact: this is a working list meant to be added to,
checked off, and cleared, not durable long-term memory.
"""

from felt_core import shopping
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "shopping_list"
description = "The shopping list: add, remove or read items (not remember_fact)."
parameters_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": "Default list",
            "enum": ["add", "remove", "list"],
        },
        "item": {"type": "string", "description": "The item"},
    },
}


async def handle(params, ctx):
    try:
        return await _handle(params, ctx.config)
    except shopping.ShoppingUnavailable:
        return SkillResult(text="Couldn't reach the shopping list in Home Assistant right now.")


async def _handle(params, config):
    # Real bug found live, 2026-09-12: "add milk to the shopping list"
    # routed to {"item": "milk"} with NO "action" key at all — defaulting
    # a missing action to "list" then silently ignored the item and just
    # read the list back. An item being present at all is a much stronger
    # signal than an unset action string, so that's checked first.
    action = str(
        get_single_param(params, "action", "command", ignore_keys=("item", "text", "name")) or ""
    ).lower()
    item = get_single_param(params, "item", "text", "name", ignore_keys=("action", "command"))

    if "remove" in action or "delete" in action:
        if not item:
            return SkillResult(text="Remove what from the shopping list?")
        listed = await shopping.items(config)
        matches = [i for i in listed if str(item).lower() in i["text"].lower()]
        if not matches:
            return SkillResult(text=f"I don't see '{item}' on the shopping list.")
        if len(matches) > 1:
            names = ", ".join(i["text"] for i in matches)
            return SkillResult(text=f"Multiple items match '{item}': {names}. Which one?")
        await shopping.remove(config, matches[0]["id"])
        return SkillResult(text=f"Removed {matches[0]['text']} from the shopping list.")

    if item and ("add" in action or not action):
        await shopping.add(config, str(item))
        return SkillResult(text=f"Added {item} to the shopping list.")

    if "add" in action and not item:
        return SkillResult(text="What should I add to the shopping list?")

    unchecked = [i for i in await shopping.items(config) if not i["checked"]]
    if not unchecked:
        return SkillResult(text="The shopping list is empty.")
    return SkillResult(text="Shopping list: " + ", ".join(i["text"] for i in unchecked))
