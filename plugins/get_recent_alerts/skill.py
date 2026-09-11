"""Lets Felt be asked about ntfy alerts conversationally, instead of only
ever being a silent log — the read half of the ntfy integration."""

from felt_core.db import get_recent_ntfy_events
from felt_core.plugin import SkillResult

name = "get_recent_alerts"
description = (
    "Checks Felt's own locally-stored log of ntfy alerts/notifications from "
    "the home server. Use this, NOT delegate_research, for any question "
    "mentioning 'alerts', 'notifications', 'ntfy', or 'anything wrong' with "
    "the server/system — this is local data, not a web search."
)
parameters_schema = {"type": "object", "properties": {}}


async def handle(params, ctx):
    events = get_recent_ntfy_events(ctx.config.db_path, limit=5)
    if not events:
        return SkillResult(text="No recent alerts.")
    lines = [f"[{e['received_at']}] {e['title'] or ''}: {e['message']}" for e in events]
    return SkillResult(text="\n".join(lines))
