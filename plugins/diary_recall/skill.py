"""WP-26: "what did I do yesterday?" / "how was my week?" - answered
straight from the diary (felt_core.diary/db.py), not a fresh model guess.
Smallest viable version: no LLM call of its own, just the stored entry/
summary text handed back for the normal skill-phrasing step to voice
naturally (plugins/skills.json marks this "public" - the diary text is
already cloud-safe by construction, WP-26's own privacy line).
"""

from datetime import UTC, datetime, timedelta

from felt_core.db import get_diary, get_latest_diary_week
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult
from felt_core.self_memory import LISBON

name = "diary_recall"
description = (
    "Mario asking Felt to recall her own diary: 'what did I do yesterday', "
    "'how was my week', 'what did we get up to', 'what's in your diary'. "
    "NOT for anything else - not general memory/facts, not calendar events."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "range": {
            "type": "string",
            "description": "one of: yesterday, week (default yesterday if unclear)",
        },
    },
}

_NO_ENTRY_TEXT = "I haven't written anything down for that yet - ask me again tomorrow."
_NO_WEEK_TEXT = "I haven't compressed a week's worth of entries yet."


def _wants_week(raw: str) -> bool:
    return "week" in raw.lower()


async def handle(params, ctx):
    raw_range = str(get_single_param(params, "range") or ctx.original_message or "")
    config = ctx.config

    if _wants_week(raw_range):
        week_row = get_latest_diary_week(config.db_path)
        if week_row is None:
            return SkillResult(text=_NO_WEEK_TEXT)
        return SkillResult(text=f"This week: {week_row['summary']}")

    yesterday = (datetime.now(UTC).astimezone(LISBON).date() - timedelta(days=1)).isoformat()
    row = get_diary(config.db_path, yesterday)
    if row is None:
        return SkillResult(text=_NO_ENTRY_TEXT)
    return SkillResult(text=row["entry"])
