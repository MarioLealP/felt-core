"""Time-of-day alarms ("wake me up at 7:30am", "alarm for 9pm") —
distinct from set_timer, which is duration-only ("in 10 minutes").

Mario, 2026-09-12: "Just use the phone alarm stuff. Its already a done
feature from Google just need a way to call it." — prefers a real alarm
in the phone's own Clock app (since 2026-10-18 through the Home Assistant
app, phone.py - the adb desktop agent died with Windows) over felt's own
SQLite-backed scheduling below, since a
phone alarm rings out loud and works even if felt-core is down.

Only used when the requested time is the *next* occurrence of that
hour:minute, though — Android's SET_ALARM intent has no "on this specific
date" extra, only a time of day, so it always means "the next time it's
this o'clock." A further-out request ("next Friday at 9am" said on a
Monday) can't be expressed that way and falls back to felt's own
persisted scheduling (alarms.py), same as when the phone can't be
reached.
"""

from datetime import datetime, timedelta

from felt_core.alarms import schedule_alarm
from felt_core.date_resolution import resolve_datetime
from felt_core.params import get_single_param
from felt_core.phone import PhoneUnavailable
from felt_core.phone import set_alarm as phone_alarm
from felt_core.plugin import SkillResult

name = "set_alarm"
description = (
    "Sets an alarm for a time of day ('7:30am', 'tomorrow at 9') on Mario's phone. Not a "
    "countdown (set_timer)."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "time": {"type": "string", "description": "The time, as Mario said it"},
        "label": {"type": "string", "description": "What it's for"},
    },
    "required": ["time"],
}


def _format(fire_at: datetime, label: str) -> str:
    formatted = fire_at.strftime("%A %I:%M %p").replace(" 0", " ")
    suffix = f" ({label})" if label else ""
    return f"{formatted}{suffix}"


def _is_next_occurrence(fire_at: datetime, now: datetime) -> bool:
    """True if `fire_at` is just "the next time it's this o'clock" from
    `now` — the only thing a phone-native alarm can actually express."""
    next_occurrence = now.replace(hour=fire_at.hour, minute=fire_at.minute, second=0, microsecond=0)
    if next_occurrence <= now:
        next_occurrence += timedelta(days=1)
    return next_occurrence.date() == fire_at.date()


async def handle(params, ctx):
    time_text = get_single_param(params, "time", "when", "alarm_time", "time_of_day")
    if not time_text:
        return SkillResult(text="I need a time to set an alarm for.")

    now = datetime.now()
    # "wake me at 6" is morning, not 18:00
    fire_at = resolve_datetime(str(time_text), now, pm_below=0)
    if fire_at is None:
        return SkillResult(text=f'I couldn\'t figure out what time "{time_text}" means.')

    label = params.get("label", "")
    config = ctx.config

    if _is_next_occurrence(fire_at, now):
        try:
            await phone_alarm(config, fire_at.hour, fire_at.minute, label)
            return SkillResult(text=f"Alarm set on your phone for {_format(fire_at, label)}.")
        except PhoneUnavailable:
            pass  # phone unreachable — fall through to felt's own scheduling

    schedule_alarm(config, fire_at, label)
    return SkillResult(text=f"Alarm set for {_format(fire_at, label)}.")
