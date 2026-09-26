"""A running shopping list — same shopping_list table a phone browser can
also check items off from directly (see api.py's /shopping-list mini-app),
not just voice. Distinct from remember_fact: this is a working list meant
to be added to, checked off, and cleared, not durable long-term memory.
"""

from felt_core.db import add_shopping_item, delete_shopping_item, get_shopping_items
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "shopping_list"
description = (
    "Adds an item to the shopping list, removes one, or reads back what's currently "
    "on it. Use this for a running list of things to buy — not remember_fact."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "'add', 'remove', or 'list' (default)"},
        "item": {"type": "string", "description": "The item to add or remove"},
    },
}


async def handle(params, ctx):
    # Real bug found live, 2026-09-12: "add milk to the shopping list"
    # routed to {"item": "milk"} with NO "action" key at all — defaulting
    # a missing action to "list" then silently ignored the item and just
    # read the list back. An item being present at all is a much stronger
    # signal than an unset action string, so that's checked first.
    action = str(
        get_single_param(params, "action", "command", ignore_keys=("item", "text", "name")) or ""
    ).lower()
    item = get_single_param(params, "item", "text", "name", ignore_keys=("action", "command"))
    db_path = ctx.config.db_path

    if "remove" in action or "delete" in action:
        if not item:
            return SkillResult(text="Remove what from the shopping list?")
        matches = [i for i in get_shopping_items(db_path) if str(item).lower() in i["text"].lower()]
        if not matches:
            return SkillResult(text=f"I don't see '{item}' on the shopping list.")
        if len(matches) > 1:
            names = ", ".join(i["text"] for i in matches)
            return SkillResult(text=f"Multiple items match '{item}': {names}. Which one?")
        delete_shopping_item(db_path, matches[0]["id"])
        return SkillResult(text=f"Removed {matches[0]['text']} from the shopping list.")

    if item and ("add" in action or not action):
        add_shopping_item(db_path, str(item))
        return SkillResult(text=f"Added {item} to the shopping list.")

    if "add" in action and not item:
        return SkillResult(text="What should I add to the shopping list?")

    unchecked = [i for i in get_shopping_items(db_path) if not i["checked"]]
    if not unchecked:
        return SkillResult(text="The shopping list is empty.")
    return SkillResult(text="Shopping list: " + ", ".join(i["text"] for i in unchecked))
