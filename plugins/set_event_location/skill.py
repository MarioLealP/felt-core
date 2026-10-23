"""Where the appointment is (Mario 2026-10-19: "Schedule x. OK scheduled!
Where?") - his answer to create_calendar_event's question. The one still
held (appointment_hold.py) goes in with the place: one add. An answer that
comes after the 30 s, when it went in without a place, changes the one
create_calendar_event made last (within 2 h) - never a guess by title.

HA can't edit an event: appointment_hold.change_place adds it again with the
place and only then deletes the old one.
"""

from datetime import datetime

from felt_core import appointment_hold
from felt_core.ha_calendar import TZ, connected, last_created
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "set_event_location"
description = (
    "Where the appointment you just asked about is - Mario's answer, e.g. 'Clínica "
    "Sorriso in Gaia'. No place ('none', 'online', 'doesn't matter'): place 'none'. A new"
    " appointment: create_calendar_event."
)
parameters_schema = {
    "type": "object",
    "properties": {"place": {"type": "string", "description": "As mario said it"}},
    "required": ["place"],
}


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

    return SkillResult(text=await appointment_hold.change_place(config, last, str(place)))
