"""Durable, cross-session memory — distinct from conversation history.

Only written via this explicit skill, never inferred silently, so what
Felt "knows" long-term is auditable: someone asked it to remember
something, it's in the facts table, done. See db.py for the schema.
"""

from felt_core.db import save_fact
from felt_core.plugin import SkillResult

name = "remember_fact"
description = (
    "Saves a durable fact that should be remembered across every future "
    "conversation, not just this one — e.g. a stated preference, a "
    "correction, a standing detail about the user. Only use this when the "
    "user is clearly asking you to remember something long-term."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "The fact to remember, in plain words"}
    },
    "required": ["content"],
}


async def handle(params, ctx):
    content = params.get("content") or ctx.original_message
    save_fact(ctx.config.db_path, content)
    return SkillResult(text=f"Remembered: {content}")
