"""Google Calendar, write side ("add a dentist appointment Friday at
3pm") — Mario, 2026-09-14: "add event creation, if you want we can just
oauth fresh, if it[']s less effort." It was: Noctalia's own stored
token turned out to be read-only, so Felt got its own dedicated OAuth
client requesting full read/write scope (see google_calendar.py).

Defaults to Mario's primary calendar; a named calendar ("add it to my
professional calendar") is matched against his real calendar list, not
guessed — and rejected up front if it turns out not to be writable
(the public-holidays calendar, for instance, is subscribed/read-only).
"""

from datetime import datetime, timedelta

import httpx

from felt_core.date_resolution import resolve_datetime
from felt_core.google_calendar import TZ, create_event, list_calendars, resolve_calendar
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "create_calendar_event"
description = (
    "Creates a new event on Mario's Google Calendar, e.g. 'add a dentist appointment "
    "Friday at 3pm'. Adds to his primary calendar by default, or a named one if he says "
    "which (e.g. 'on my professional calendar')."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "What the event is called"},
        "when": {
            "type": "string",
            "description": (
                "The date and time, exactly as phrased ('friday at 3pm', 'tomorrow at "
                "9am'). Never reformat or invent a value."
            ),
        },
        "duration_minutes": {
            "type": "integer",
            "description": "How long the event lasts, in minutes. Defaults to 60 if not mentioned.",
        },
        "calendar": {
            "type": "string",
            "description": "Which calendar to add it to, if Mario names one. Defaults to his primary calendar.",
        },
    },
    "required": ["title", "when"],
}


async def handle(params, ctx):
    config = ctx.config
    if not config.google_calendar_refresh_token:
        return SkillResult(text="Google Calendar isn't connected yet.")

    title = get_single_param(params, "title", ignore_keys=("when", "duration_minutes", "calendar"))
    when_text = get_single_param(params, "when", ignore_keys=("title", "duration_minutes", "calendar"))
    if not title or not when_text:
        return SkillResult(text="I need both a title and a time to create a calendar event.")

    duration_minutes = params.get("duration_minutes") or 60
    calendar_name = params.get("calendar")

    now = datetime.now(tz=TZ)
    start = resolve_datetime(str(when_text), now)
    if start is None:
        return SkillResult(text=f"I couldn't figure out what time '{when_text}' means.")
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    end = start + timedelta(minutes=duration_minutes)

    async with httpx.AsyncClient(timeout=20) as client:
        calendars = await list_calendars(client, config)
        calendar = resolve_calendar(calendars, calendar_name)
        if calendar is None:
            target = f"'{calendar_name}'" if calendar_name else "your primary calendar"
            return SkillResult(text=f"Couldn't find {target} to add the event to.")
        if calendar.get("accessRole") not in ("owner", "writer"):
            return SkillResult(
                text=f"'{calendar.get('summary', calendar['id'])}' is read-only — I can't add events to it."
            )

        await create_event(client, config, calendar["id"], str(title), start, end)

    formatted = start.strftime("%A %I:%M %p").replace(" 0", " ")
    return SkillResult(
        text=f"Added '{title}' to {calendar.get('summary', 'your calendar')} for {formatted}."
    )
