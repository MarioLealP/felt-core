"""Where the appointment is (Mario 2026-10-19: "Schedule x. OK scheduled!
Where?") - his answer to create_calendar_event's question. The one still
held (appointment_hold.py) goes in with the place: one add. An answer that
comes after the 30 s, when it went in without a place, changes the one
create_calendar_event made last (within 2 h) - never a guess by title.

HA can't edit an event, so a change adds the appointment again with the
place and only then deletes the old one: a failure halfway leaves a
duplicate, never a lost appointment.
"""

from datetime import datetime, timedelta

import httpx

from felt_core import appointment_hold
from felt_core.ha_calendar import (
    TZ,
    CalendarUnavailable,
    connected,
    create_event,
    delete_event,
    event_start,
    last_created,
    list_events,
    refresh,
    remember_created,
)
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "set_event_location"
description = (
    "Sets where the appointment Mario just gave you is - his answer after you asked where "
    "it is, e.g. 'Clínica Sorriso in Gaia', 'at Hospital da Luz'. If he says there's no "
    "place ('none', 'online', 'doesn't matter'), call it with place 'none'. Only for that "
    "appointment; for a new appointment use create_calendar_event."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "place": {
            "type": "string",
            "description": "Where it is - a place name and/or address, exactly as Mario said it.",
        },
    },
    "required": ["place"],
}


def _same(event: dict, summary: str, start: datetime) -> bool:
    return event.get("summary") == summary and event_start(event) == start


async def _matching(config, calendar_id: str, summary: str, start: datetime) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        await refresh(client, config, calendar_id)
        events = await list_events(client, config, calendar_id, start, start + timedelta(minutes=1))
    return [e for e in events if _same(e, summary, start)]


async def handle(params, ctx):
    config = ctx.config
    if not connected(config):
        return SkillResult(text="The calendar isn't connected yet.")
    place = get_single_param(params, "place", "location", "where")
    if not place:
        return SkillResult(text="I need the place.")
    place = None if appointment_hold.no_place(str(place)) else str(place)
    held = appointment_hold.claim()
    if held is not None:
        if not await appointment_hold.add(config, held, place):
            return SkillResult(text="I couldn't reach the calendar to add it.")
        added = f"Added '{held['summary']}', {appointment_hold.when(held['start'])}"
        where = f"at {place}" if place else "no place"
        return SkillResult(text=f"{added}, {where}. Read it back to Mario as written.")
    last = last_created(config, datetime.now(tz=TZ))
    if place is None:
        if last is None:
            return SkillResult(text="Nothing to change.")
        return SkillResult(text=f"'{last['summary']}' stays without a place.")
    if last is None:
        return SkillResult(
            text="There's no appointment I just added - tell Mario to say the whole thing again "
            "with the place in it."
        )

    calendar_id, summary = last["calendar"], last["summary"]
    start = datetime.fromisoformat(last["start"])
    end = datetime.fromisoformat(last["end"])
    try:
        old = await _matching(config, calendar_id, summary, start)
        await create_event(config, calendar_id, summary, start, end, location=str(place))
        new = [
            e
            for e in await _matching(config, calendar_id, summary, start)
            if e["uid"] not in {o["uid"] for o in old}
        ]
        if not new:
            return SkillResult(text="I couldn't confirm the change in the calendar.")
        for event in old:
            await delete_event(config, calendar_id, event["uid"])
    except (CalendarUnavailable, httpx.HTTPError):
        return SkillResult(text="I couldn't reach the calendar to add the place.")
    remember_created(config, calendar_id, summary, start, end)
    return SkillResult(
        text=f"'{summary}', {appointment_hold.when(start)}, is now at {place}. Read it back "
        "to Mario as written."
    )
