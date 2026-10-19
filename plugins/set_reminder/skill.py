"""Reminders (WP-46): "remind me Friday at 3 to call the dentist", "every
Monday at 9 remind me to take the bins out". Each one is an event in
the "Felt reminders" calendar, through Home Assistant (Mario 2026-10-18:
linked to the phone, not a Felt-only list) - that calendar's own 0-minute
notification makes the phone's Calendar app ring right on time, recurring
ones repeat there, and the main calendar's time isn't filled. Being in that
calendar is what makes calendar_headsup speak it at the desk on time too,
instead of the 30/10-minute heads-ups.
"""

import re
from datetime import datetime, timedelta

from felt_core.date_resolution import resolve_datetime
from felt_core.ha_calendar import TZ, CalendarUnavailable, connected, create_event
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "set_reminder"
description = (
    "Reminds Mario of something at a date/time, once or repeating ('remind me Friday at "
    "3pm to call the dentist', 'every Monday at 9 remind me to...'). His phone notifies "
    "him. A plain countdown: set_timer; waking up: set_alarm."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "what": {"type": "string", "description": "What to remind him of"},
        "when": {"type": "string", "description": "The (first) date and time, as Mario said it"},
        "repeat": {
            "type": "string",
            "description": "Default none; weekly = same weekday",
            "enum": ["none", "daily", "weekdays", "weekly", "monthly"],
        },
    },
    "required": ["what", "when"],
}

_RRULE = {
    "daily": "FREQ=DAILY",
    "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "weekly": "FREQ=WEEKLY",
    "monthly": "FREQ=MONTHLY",
}
_EVERY = {
    "daily": "every day",
    "weekdays": "every weekday",
    "weekly": "every {day}",
    "monthly": "every month on the {date}",
}


_EVERY_WHEN = re.compile(r"^\s*(?:every|each)\s+(?P<rest>.+)$", re.IGNORECASE)
# a time of day in the text ("at 9", "3pm", "15:30", "noon", "in 20 minutes")
_HAS_CLOCK = re.compile(
    r"\d\s*(?:am|pm|a\.m|p\.m)\b|\d:\d|\bat\s+\d|\b(?:noon|midnight|morning|evening|tonight)\b"
    r"|\bin\s+\d+\s*(?:mins?|minutes?|hours?|h)\b|\bin\s+an?\s+(?:hour|minute)",
    re.IGNORECASE,
)
_DAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"


def split_every(when: str, repeat: str) -> tuple[str, str]:
    """Pure: 'every monday at 9' -> ('monday at 9', 'weekly'). felt-llm
    sometimes leaves the repeat in the time text (e2e 2026-10-18); the date
    parser can't read 'every', and an unset repeat is taken from it."""
    m = _EVERY_WHEN.match(when)
    if m is None:
        return when, repeat
    rest = m["rest"]
    found = "none"
    if re.match(r"(?:day|night|morning|evening)\b", rest, re.IGNORECASE):
        found, rest = "daily", re.sub(r"^\w+\s*", "", rest) or "today"
    elif re.match(r"week\s*days?\b", rest, re.IGNORECASE):
        found, rest = "weekdays", re.sub(r"^week\s*days?\s*", "", rest) or "today"
    elif re.match(rf"(?:{_DAYS})s?\b", rest, re.IGNORECASE):
        found, rest = "weekly", re.sub(r"^(\w+?)s?\b", r"\1", rest)
    elif re.match(r"month\b", rest, re.IGNORECASE):
        found, rest = "monthly", re.sub(r"^month\s*", "", rest) or "today"
    rest = re.sub(r"^(?:at|on)\s+", "", rest.strip()) if found in ("daily", "weekdays") else rest
    if found in ("daily", "weekdays", "monthly") and not re.search(r"\d", rest):
        rest = "today"
    elif found in ("daily", "weekdays"):
        rest = f"today at {rest}" if not rest.lower().startswith("today") else rest
    return rest.strip(), repeat if repeat in _RRULE else found


def describe(start: datetime, repeat: str) -> str:
    """Pure: 'Friday 3:00 PM' / 'every Monday at 9:00 AM'."""
    clock = start.strftime("%I:%M %p").lstrip("0")
    if repeat in _EVERY:
        every = _EVERY[repeat].format(day=start.strftime("%A"), date=_ordinal(start.day))
        return f"{every} at {clock}"
    return f"{start.strftime('%A')} {clock}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


async def handle(params, ctx):
    config = ctx.config
    if not connected(config):
        return SkillResult(text="The calendar isn't connected yet.")

    what = get_single_param(params, "what", "title", "text", ignore_keys=("when", "repeat"))
    when_text = get_single_param(params, "when", "time", ignore_keys=("what", "repeat"))
    if not what or not when_text:
        return SkillResult(text="I need what to remind you about and when.")
    repeat = str(params.get("repeat") or "none").lower()
    if repeat not in _RRULE:
        repeat = "none"

    when_text, repeat = split_every(str(when_text), repeat)
    if not _HAS_CLOCK.search(when_text):
        # "for the 21st" - a day, no time: morning, not midnight (2026-10-18)
        when_text = f"{when_text} at 9:00"
    now = datetime.now(tz=TZ)
    start = resolve_datetime(when_text, now)
    if start is None:
        return SkillResult(text=f"I couldn't figure out what time '{when_text}' means.")
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    if repeat != "none" and start <= now:
        start += timedelta(days=1)  # "every day at 9am" said at 10am: from tomorrow
    if start <= now and repeat == "none":
        return SkillResult(text=f"'{when_text}' is already past.")

    try:
        await create_event(
            config,
            config.ha_calendar_reminders,
            str(what),
            start,
            start + timedelta(minutes=15),
            "Reminder set by Felt.",
            rrule=_RRULE.get(repeat),
        )
    except CalendarUnavailable:
        return SkillResult(text="I couldn't reach the calendar to set that reminder.")
    return SkillResult(text=f"I'll remind you {describe(start, repeat)}: {what}.")
