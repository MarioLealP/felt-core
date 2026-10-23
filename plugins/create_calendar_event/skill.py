"""Calendar, write side ("add a dentist appointment Friday at 3pm") —
Mario, 2026-09-14: "add event creation". Through Home Assistant since
2026-10-19 (ha_calendar.py).

Defaults to Mario's primary calendar; a named calendar ("add it to my
professional calendar") is matched against his real calendar list, not
guessed — and rejected up front if it turns out not to be writable
(the public-holidays calendar, for instance, is subscribed/read-only).

The place is optional. Without one she holds the appointment and asks
where it is (Mario 2026-10-19: one add, not two; "If no reply in 30s
create"): his answer goes to set_event_location, which adds it with the
place, and with no answer it goes in without one (appointment_hold.py). A
place is what gives an appointment leave-by nudges instead of plain
heads-ups.
"""

from datetime import datetime, timedelta

import httpx

from felt_core import appointment_hold
from felt_core.date_resolution import day_only, resolve_datetime
from felt_core.ha_calendar import (
    TZ,
    CalendarUnavailable,
    connected,
    create_event,
    list_calendars,
    remember_created,
    resolve_calendar,
    writable,
)
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "create_calendar_event"
description = "Adds an appointment or event to Mario's calendar, e.g. 'dentist Friday at 3pm'."
parameters_schema = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "What it is"},
        "when": {"type": "string", "description": "Date and time, as Mario said it"},
        "duration_minutes": {"type": "integer", "description": "Length in minutes (default 60)"},
        "calendar": {"type": "string", "description": "Calendar name, only if he names one"},
        "place": {
            "type": "string",
            "description": "Where, as Mario said it; leave out if he didn't say",
        },
    },
    "required": ["title", "when"],
}


async def handle(params, ctx):
    config = ctx.config
    if not connected(config):
        return SkillResult(text="The calendar isn't connected yet.")

    title = get_single_param(
        params, "title", ignore_keys=("when", "duration_minutes", "calendar", "place")
    )
    when_text = get_single_param(
        params, "when", ignore_keys=("title", "duration_minutes", "calendar", "place")
    )
    if not title or not when_text:
        return SkillResult(text="I need both a title and a time to create a calendar event.")

    duration_minutes = params.get("duration_minutes") or 60
    calendar_name = params.get("calendar")
    said = str(params.get("place") or "").strip()
    place = None if not said or appointment_hold.no_place(said) else said

    now = datetime.now(tz=TZ)
    start = resolve_datetime(str(when_text), now)
    if start is None:
        return SkillResult(text=f"I couldn't figure out what time '{when_text}' means.")
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    # a day with no time ("4th November is the WoW release"): an all-day event,
    # not an hour at midnight, and no place to ask for (cm-0vl)
    all_day = day_only(str(when_text), ctx.original_message or "")
    if all_day:
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    else:
        end = start + timedelta(minutes=duration_minutes)

    # called again for the appointment that's waiting for its place: his answer
    # (e2e 2026-10-19: after "none" felt-llm re-called it without any place)
    waiting = appointment_hold.claim_same(start)
    if waiting is not None:
        if not await appointment_hold.add(config, waiting, place):
            return SkillResult(text="I couldn't reach the calendar to add it.")
        added = f"Added '{waiting['summary']}', {appointment_hold.when(start)}"
        where = f"at {place}" if place else "no place"
        return SkillResult(text=f"{added}, {where}. Read it back to Mario as written.")

    async with httpx.AsyncClient(timeout=20) as client:
        calendars = await list_calendars(client, config)
        calendar = resolve_calendar(calendars, calendar_name)
        if calendar is None:
            target = f"'{calendar_name}'" if calendar_name else "your primary calendar"
            return SkillResult(text=f"Couldn't find {target} to add the event to.")
        if not await writable(client, config, calendar["id"]):
            return SkillResult(
                text=f"'{calendar.get('summary', calendar['id'])}' is read-only — I can't add events to it."
            )

    at = appointment_hold.when(start)
    where = calendar.get("summary", "your calendar")
    if all_day:
        try:
            await create_event(
                config, calendar["id"], str(title), start, end, location=place, all_day=True
            )
        except CalendarUnavailable:
            return SkillResult(text="I couldn't reach the calendar to add it.")
        day = f"{start.strftime('%A')} {start.day} {start.strftime('%B')}"
        return SkillResult(
            text=f"Added '{title}', {day}, all day, to {where}. Read it back to Mario as written."
        )
    if not said:
        appointment_hold.hold(config, calendar["id"], str(title), start, end)
        return SkillResult(
            text=f"Not added yet: '{title}', {at}, for {where}. Ask Mario where it is - don't "
            f"say it's added. If he doesn't answer within {appointment_hold.PLACE_WAIT_S} "
            "seconds it's added without a place."
        )
    try:
        await create_event(config, calendar["id"], str(title), start, end, location=place)
    except CalendarUnavailable:
        return SkillResult(text="I couldn't reach the calendar to add it.")
    remember_created(config, calendar["id"], str(title), start, end)
    spot = f"at {place}" if place else "no place"
    return SkillResult(
        text=f"Added '{title}', {at}, {spot}, to {where}. Read it back to Mario as written."
    )
