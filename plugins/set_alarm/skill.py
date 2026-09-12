"""Time-of-day alarms ("wake me up at 7:30am", "alarm for 9pm") —
distinct from set_timer, which is duration-only ("in 10 minutes").

Mario, 2026-09-12: "Just use the phone alarm stuff. Its already a done
feature from Google just need a way to call it." — prefers a real alarm
in the phone's own Clock app (via felt-desktop-agent's adb-based
phone_alarm.py) over felt's own SQLite-backed scheduling below, since a
phone alarm rings out loud and works even if felt-core is down.

Only used when the requested time is the *next* occurrence of that
hour:minute, though — Android's SET_ALARM intent has no "on this specific
date" extra, only a time of day, so it always means "the next time it's
this o'clock." A further-out request ("next Friday at 9am" said on a
Monday) can't be expressed that way and falls back to felt's own
persisted scheduling (alarms.py), same as when no desktop agent is
configured/reachable at all, or the phone can't be reached (adb down).
"""

from datetime import datetime, timedelta

import httpx

from felt_core.alarms import schedule_alarm
from felt_core.date_resolution import resolve_datetime
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "set_alarm"
description = (
    "Sets an alarm for a specific time of day (e.g. '7:30am', 'tomorrow at 9am'), "
    "not a duration — rings on the phone directly when possible, otherwise sends a "
    "push notification when it fires."
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
    fire_at = resolve_datetime(str(time_text), now)
    if fire_at is None:
        return SkillResult(text=f'I couldn\'t figure out what time "{time_text}" means.')

    label = params.get("label", "")
    config = ctx.config

    if config.desktop_agent_url and _is_next_occurrence(fire_at, now):
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                response = await client.post(
                    f"{config.desktop_agent_url}/v1/phone/alarm",
                    json={"hour": fire_at.hour, "minute": fire_at.minute, "label": label},
                )
                if response.status_code == 200:
                    return SkillResult(text=f"Alarm set on your phone for {_format(fire_at, label)}.")
            except httpx.HTTPError:
                pass  # phone unreachable — fall through to felt's own scheduling

    schedule_alarm(config, fire_at, label)
    return SkillResult(text=f"Alarm set for {_format(fire_at, label)}.")
