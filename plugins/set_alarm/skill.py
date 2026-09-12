"""Time-of-day alarms ("wake me up at 7:30am", "alarm for 9pm") —
distinct from set_timer, which is duration-only ("in 10 minutes"). Mario,
2026-09-12: "another one skill i remembered was set an alarm / timer",
naming this as a second, separate capability rather than an extension of
set_timer.
"""

from datetime import datetime

from felt_core.alarms import schedule_alarm
from felt_core.date_resolution import resolve_datetime
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "set_alarm"
description = (
    "Sets an alarm for a specific time of day (e.g. '7:30am', 'tomorrow at 9am'), "
    "not a duration — sends a push notification when it fires."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "time": {
            "type": "string",
            "description": "The time of day the alarm should fire, e.g. '7:30am' or 'tomorrow at 9am'",
        },
        "label": {"type": "string", "description": "What the alarm is for"},
    },
    "required": ["time"],
}


async def handle(params, ctx):
    time_text = get_single_param(params, "time", "when", "alarm_time", "time_of_day")
    if not time_text:
        return SkillResult(text="I need a time to set an alarm for.")

    fire_at = resolve_datetime(str(time_text), datetime.now())
    if fire_at is None:
        return SkillResult(text=f'I couldn\'t figure out what time "{time_text}" means.')

    label = params.get("label", "")
    schedule_alarm(ctx.config, fire_at, label)

    formatted = fire_at.strftime("%A %I:%M %p").replace(" 0", " ")
    suffix = f" ({label})" if label else ""
    return SkillResult(text=f"Alarm set for {formatted}{suffix}.")
