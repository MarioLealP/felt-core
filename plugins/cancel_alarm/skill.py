"""Cancels a pending alarm — set_alarm had no way to undo one, so a
mis-set or no-longer-needed alarm could only be waited out."""

from datetime import datetime

from felt_core.alarms import cancel_alarm
from felt_core.db import get_pending_alarms
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "cancel_alarm"
description = (
    "Cancels a pending alarm that hasn't fired yet. If more than one alarm is set, "
    "say which one by its label or time."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "which": {
            "type": "string",
            "description": "Which alarm to cancel, by label (optional if only one alarm is set)",
        }
    },
}


def _format(alarm: dict) -> str:
    formatted = datetime.fromisoformat(alarm["fire_at"]).strftime("%A %I:%M %p").replace(" 0", " ")
    suffix = f" ({alarm['label']})" if alarm["label"] else ""
    return f"{formatted}{suffix}"


async def handle(params, ctx):
    pending = get_pending_alarms(ctx.config.db_path)
    if not pending:
        return SkillResult(text="No alarms are currently set.")

    which = get_single_param(params, "which", "label", "alarm")
    if which:
        matches = [a for a in pending if which.lower() in a["label"].lower()]
        if not matches:
            return SkillResult(text=f"I don't see an alarm matching '{which}'.")
    else:
        matches = pending

    if len(matches) > 1:
        return SkillResult(text="Which one? You have: " + "; ".join(_format(a) for a in matches))

    alarm = matches[0]
    cancel_alarm(ctx.config, alarm["id"])
    return SkillResult(text=f"Cancelled: {_format(alarm)}.")
