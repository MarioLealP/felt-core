"""Durable, cross-session memory — distinct from conversation history.

Only written via this explicit skill, never inferred silently, so what
Felt "knows" long-term is auditable: someone asked it to remember
something, it's in the facts table, done. See db.py for the schema.
"""

from felt_core.db import save_fact
from felt_core.plugin import SkillResult

name = "remember_fact"
description = (
    "Saves a fact for good (a preference, a correction, a standing detail) - only when "
    "Mario clearly asks you to remember it."
)
parameters_schema = {
    "type": "object",
    "properties": {"content": {"type": "string", "description": "The fact, in plain words"}},
    "required": ["content"],
}


async def handle(params, ctx):
    content = params.get("content") or ctx.original_message
    save_fact(ctx.config.db_path, content)
    return SkillResult(text=f"Remembered: {content}")
