"""An appointment waiting for its place (Mario 2026-10-19: ask where first,
"If no reply in 30s create"). create_calendar_event without a place holds
it here and she asks where; set_event_location adds it with the answer (or
at once, without a place, on "none"), or after PLACE_WAIT_S it goes in
without one and she says so in a short line - if he missed her question,
that's how he learns it has no place. One add either way - whoever claims
it first. An answer after that goes through set_event_location's change
path (create-then-delete).

Held in memory: a felt-core restart inside the 30 s loses it.
"""

import asyncio
import logging
import re
from datetime import datetime

from felt_core import ha_calendar
from felt_core.config import FeltConfig
from felt_core.her_line import her_line
from felt_core.proactive import enqueue_proactive

logger = logging.getLogger(__name__)

PLACE_WAIT_S = 30
# "no place": the appointment goes in now, without one
_NO_PLACE = re.compile(
    r"^\W*(?:none|no(?:pe)?|nowhere|no (?:place|location|address)|n/?a|online|"
    r"it'?s online|doesn'?t matter|skip|not needed)\W*$",
    re.IGNORECASE,
)

_held: dict | None = None
_timer: asyncio.Task | None = None
# strong refs: asyncio only keeps weak references to bare tasks
_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def hold(config: FeltConfig, calendar_id: str, summary: str, start: datetime, end: datetime):
    """Hold an appointment for its place; an earlier one still held goes in now."""
    global _held, _timer
    earlier = claim()
    if earlier is not None:
        _spawn(_add_and_say(config, earlier))
    _held = {"calendar": calendar_id, "summary": summary, "start": start, "end": end}
    _timer = _spawn(_add_after_wait(config))


def no_place(answer: str) -> bool:
    """Pure: his answer means there's no place."""
    return bool(_NO_PLACE.match(answer.strip()))


def claim_same(start: datetime) -> dict | None:
    """Take the held appointment if it's at `start`: create_calendar_event
    called again for it (e2e 2026-10-19: felt-llm answered "none" that way)
    is his answer, not a second appointment."""
    if _held is None or _held["start"] != start:
        return None
    return claim()


def claim() -> dict | None:
    """Take the held appointment (and stop its timer), if there is one."""
    global _held, _timer
    held, _held = _held, None
    if _timer is not None and _timer is not asyncio.current_task():
        _timer.cancel()
    _timer = None
    return held


def when(start: datetime) -> str:
    """Pure: 'Tuesday 20 October, 11:00' - how she reads an appointment back."""
    local = start.astimezone(ha_calendar.TZ)
    return f"{local.strftime('%A')} {local.day} {local.strftime('%B, %H:%M')}"


async def add(config: FeltConfig, held: dict, place: str | None) -> bool:
    """Add the appointment; False if HA couldn't."""
    try:
        await ha_calendar.create_event(
            config, held["calendar"], held["summary"], held["start"], held["end"], location=place
        )
    except ha_calendar.CalendarUnavailable:
        logger.warning("appointment hold: couldn't add %r", held["summary"])
        return False
    ha_calendar.remember_created(
        config, held["calendar"], held["summary"], held["start"], held["end"]
    )
    return True


async def add_unanswered(config: FeltConfig) -> None:
    """No answer came: add what's held without a place, and say so."""
    held = claim()
    if held is not None:
        await _add_and_say(config, held)


async def _add_and_say(config: FeltConfig, held: dict) -> None:
    """Add without a place; he isn't waiting on a reply, so the outcome is a
    proactive line either way."""
    title, at = held["summary"], when(held["start"])
    if await add(config, held, None):
        facts = (
            f'You asked Mario where "{title}" ({at}) is and he didn\'t answer, so you added it '
            "to his calendar without a place - say it's in, and that it has no place."
        )
        line = await her_line(config, facts, f"Added {title}, {at}, no place.")
    else:
        facts = (
            f'You tried to add "{title}" ({at}) to Mario\'s calendar and it failed - ask him '
            "to tell you again."
        )
        fallback = f"I couldn't add {title} ({at}) to your calendar - can you tell me again?"
        line = await her_line(config, facts, fallback)
    enqueue_proactive(config, "calendar", line)


async def _add_after_wait(config: FeltConfig) -> None:
    await asyncio.sleep(PLACE_WAIT_S)
    await add_unanswered(config)
