"""Lists pending (not yet fired) alarms — set_alarm had no way to check
what's still scheduled, a real gap once more than one might be set."""

from datetime import datetime

from felt_core.db import get_pending_alarms
from felt_core.plugin import SkillResult

name = "list_alarms"
description = "Lists alarms that are set and haven't fired yet."
parameters_schema = {"type": "object", "properties": {}}


async def handle(params, ctx):
    del params
    pending = get_pending_alarms(ctx.config.db_path)
    if not pending:
        return SkillResult(text="No alarms are currently set.")

    lines = []
    for alarm in pending:
        formatted = datetime.fromisoformat(alarm["fire_at"]).strftime("%A %I:%M %p").replace(" 0", " ")
        suffix = f" ({alarm['label']})" if alarm["label"] else ""
        lines.append(f"{formatted}{suffix}")
    return SkillResult(text="Alarms set: " + "; ".join(lines))
