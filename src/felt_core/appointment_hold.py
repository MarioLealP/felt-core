"""An appointment waiting for its place (Mario 2026-10-19: ask where first,
"If no reply in 30s create"). create_calendar_event without a place holds
it here and she asks where; set_event_location adds it with the answer (or
at once, without a place, on "none"), or after PLACE_WAIT_S it goes in
without one and she says so in a short line - if he missed her question,
that's how he learns it has no place. One add either way - whoever claims
it first. An answer after that goes through change_place (create-then-delete),
from set_event_location or from create_calendar_event called again for the
appointment just added (cm-2hu: never a second one).

The 30 s start once her reply (the question) is out - chat() calls
start_wait - not at the request: a slow reply used to eat the wait (cm-a4b).
Held in memory: a felt-core restart inside the 30 s loses it.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta

import httpx

from felt_core import ha_calendar
from felt_core.config import FeltConfig
from felt_core.her_line import her_line
from felt_core.proactive import enqueue_proactive

logger = logging.getLogger(__name__)

PLACE_WAIT_S = 30
# create_calendar_event again at the start of the one added this recently = the same one
JUST_ADDED = timedelta(minutes=10)
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
    """Hold an appointment for its place; an earlier one still held goes in now.
    The wait starts at start_wait, once she has asked."""
    global _held
    earlier = claim()
    if earlier is not None:
        _spawn(_add_and_say(config, earlier))
    _held = {"calendar": calendar_id, "summary": summary, "start": start, "end": end}


def start_wait(config: FeltConfig) -> None:
    """Her reply is out: start the 30 s for what's held, if its wait hasn't started."""
    global _timer
    if _held is not None and _timer is None:
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


def just_added(config: FeltConfig, summary: str, start: datetime) -> dict | None:
    """The appointment create_calendar_event added in the last 10 min, if it's
    this one (`start`, and the title one inside the other: "dentist" / "Dentist
    appointment") - being called again for it is his answer, not a second one."""
    now = datetime.now(ha_calendar.TZ)
    last = ha_calendar.last_created(config, now)
    if last is None or datetime.fromisoformat(last["start"]) != start:
        return None
    a, b = summary.casefold().strip(), last["summary"].casefold().strip()
    if a not in b and b not in a:
        return None
    if now - datetime.fromisoformat(last["at"]) > JUST_ADDED:
        return None
    return last


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


def _same(event: dict, summary: str, start: datetime) -> bool:
    return event.get("summary") == summary and ha_calendar.event_start(event) == start


async def _matching(config, calendar_id: str, summary: str, start: datetime) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        await ha_calendar.refresh(client, config, calendar_id)
        events = await ha_calendar.list_events(
            client, config, calendar_id, start, start + timedelta(minutes=1)
        )
    return [e for e in events if _same(e, summary, start)]


async def change_place(config: FeltConfig, last: dict, place: str) -> str:
    """Give the appointment already added (`last`, from remember_created) a place.
    HA can't edit an event, so the one with the place goes in first and the old
    one is deleted only after: a failure halfway leaves a duplicate, never a lost
    appointment. Returns what to tell her."""
    calendar_id, summary = last["calendar"], last["summary"]
    start = datetime.fromisoformat(last["start"])
    end = datetime.fromisoformat(last["end"])
    try:
        old = await _matching(config, calendar_id, summary, start)
        await ha_calendar.create_event(config, calendar_id, summary, start, end, location=place)
        new = [
            e
            for e in await _matching(config, calendar_id, summary, start)
            if e["uid"] not in {o["uid"] for o in old}
        ]
        if not new:
            return "I couldn't confirm the change in the calendar."
        for event in old:
            await ha_calendar.delete_event(config, calendar_id, event["uid"])
    except (ha_calendar.CalendarUnavailable, httpx.HTTPError):
        return "I couldn't reach the calendar to add the place."
    ha_calendar.remember_created(config, calendar_id, summary, start, end)
    return f"'{summary}', {when(start)}, is now at {place}. Read it back to Mario as written."


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
