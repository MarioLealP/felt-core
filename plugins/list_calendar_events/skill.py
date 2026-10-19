"""Calendar, read side ("what's on my calendar today/tomorrow/on Friday").
Checks every calendar Mario has in Home Assistant (both accounts, holidays,
birthdays, Felt's reminders - Mario, 2026-09-14: "get all calendars"), not
just the primary one, since a holiday or a professional-calendar meeting is
just as real an answer as a personal-calendar event. Through HA since
2026-10-19 (ha_calendar.py).
"""

from datetime import datetime

import httpx

from felt_core.date_resolution import resolve_date
from felt_core.ha_calendar import (
    TZ,
    connected,
    day_bounds,
    list_calendars,
    list_events,
    summarize_events,
)
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "list_calendar_events"
description = (
    "Lists what's on Mario's calendar for a given day — defaults to today if no "
    "day is mentioned. Covers all of his calendars, not just one."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "day": {
            "type": "string",
            "description": (
                "Which day to check, exactly as phrased ('today', 'tomorrow', 'friday', "
                "a specific date). Never reformat or invent a value. Defaults to today."
            ),
        },
    },
}


async def handle(params, ctx):
    config = ctx.config
    if not connected(config):
        return SkillResult(text="The calendar isn't connected yet.")

    day_text = get_single_param(params, "day", "when", "date")
    today = datetime.now(tz=TZ).date()
    resolved = (resolve_date(str(day_text), today) if day_text else None) or today
    time_min, time_max = day_bounds(resolved)

    async with httpx.AsyncClient(timeout=20) as client:
        calendars = await list_calendars(client, config)
        events_by_calendar = []
        for calendar in calendars:
            events = await list_events(client, config, calendar["id"], time_min, time_max)
            events_by_calendar.append((calendar.get("summary", calendar["id"]), events))

    return SkillResult(text=summarize_events(events_by_calendar))
