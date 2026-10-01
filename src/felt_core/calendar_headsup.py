"""WP-31: calendar heads-up. A background loop polls every Google Calendar
Mario has (same set as the list_calendar_events skill) every 5 min for
the next 2 h and schedules a proactive (WP-25) heads-up per event at
T-30 min (`calendar`, normal) and T-10 min (`urgent` - felt-tray delivers
those even while she's hidden or away).

Each heads-up is timed to the second with its own sleep rather than
waiting for the next poll, and re-fetches the event right before it's
phrased: a deleted event is dropped, and a moved one is dropped too
(the next poll schedules it at its new time, under a new dedupe key).
Sent heads-ups are remembered in the settings table, keyed on event id
+ start time + stage, so a restart doesn't repeat them. All-day events
and ones Mario declined are skipped. Explicit alarms (alarms.py) are
untouched - this is only a gentle reminder.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from felt_core import db, google_calendar, local_llm
from felt_core.config import FeltConfig
from felt_core.events import _game_name
from felt_core.proactive import enqueue_proactive

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 5 * 60
LOOKAHEAD = timedelta(hours=2)
# The 30-min heads-up stays useful for 15 min at most (the 10-min one
# takes over after that); the urgent one is useless once the event starts.
_NORMAL_TTL_S = 15 * 60
# How far back to look for a game_start that hasn't been stopped yet.
_GAME_CONTEXT_WINDOW = timedelta(hours=6)


@dataclass(frozen=True)
class Stage:
    name: str
    lead_s: int
    kind: str


NORMAL = Stage("30", 30 * 60, "calendar")
URGENT = Stage("10", 10 * 60, "urgent")
STAGES = (NORMAL, URGENT)

# Keys scheduled in this process but not yet delivered - stops the next
# poll from scheduling the same heads-up a second time.
_pending: set[str] = set()
# Strong refs: asyncio only keeps weak references to bare tasks.
_tasks: set[asyncio.Task] = set()


def event_start(event: dict) -> datetime | None:
    """Pure: the event's start as an aware datetime, or None for an
    all-day event (Google gives those a `date`, not a `dateTime`)."""
    raw = event.get("start", {}).get("dateTime")
    return datetime.fromisoformat(raw) if raw else None


def is_relevant(event: dict) -> bool:
    """Pure: a timed, not-cancelled event Mario hasn't declined."""
    if event.get("status") == "cancelled" or event_start(event) is None:
        return False
    return not any(
        a.get("self") and a.get("responseStatus") == "declined" for a in event.get("attendees", [])
    )


def due_stage(start: datetime, now: datetime) -> Stage | None:
    """Pure: which heads-up is right for `now` - URGENT inside the last
    10 min, NORMAL between 30 and 10 min out, else none. A late first
    sight (8 min out) skips the 30-min one rather than sending both."""
    left = (start - now).total_seconds()
    if left <= 0:
        return None
    if left <= URGENT.lead_s:
        return URGENT
    if left <= NORMAL.lead_s:
        return NORMAL
    return None


def dedupe_key(event: dict, stage: Stage) -> str:
    start = event_start(event)
    return f"calendar_headsup:{event.get('id')}|{start.isoformat() if start else ''}|{stage.name}"


def already_sent(config: FeltConfig, key: str) -> bool:
    return db.get_setting(config.db_path, key) is not None


def plan(
    events: list[tuple[str, dict]], now: datetime, horizon_s: float, sent
) -> list[tuple[float, str, dict, Stage]]:
    """Pure (given `sent`): the heads-ups to schedule from this poll, as
    (delay_s, calendar_id, event, stage) - every stage whose fire time is
    before the next poll (`horizon_s`) and that isn't sent or pending.
    A fire time already past gets delay 0; `deliver` then re-checks with
    `due_stage`, so a stale stage is dropped rather than sent late."""
    out: list[tuple[float, str, dict, Stage]] = []
    for calendar_id, event in events:
        if not is_relevant(event):
            continue
        start = event_start(event)
        assert start is not None
        for stage in STAGES:
            key = dedupe_key(event, stage)
            if key in _pending or sent(key):
                continue
            delay = (start - timedelta(seconds=stage.lead_s) - now).total_seconds()
            if delay <= horizon_s and (start - now).total_seconds() > 0:
                out.append((max(0.0, delay), calendar_id, event, stage))
    return out


def minutes_left(start: datetime, now: datetime) -> int:
    return max(1, round((start - now).total_seconds() / 60))


def fallback_line(title: str, minutes: int) -> str:
    return f"heads up, {title} in {minutes} minutes."


def current_game(config: FeltConfig, now: datetime) -> str | None:
    """The game Mario's in right now per felt-animation's event log (the
    latest game_start with no game_stop after it), else None."""
    fmt = "%Y-%m-%d %H:%M:%S"
    rows = db.get_events_between(
        config.db_path,
        (now - _GAME_CONTEXT_WINDOW).astimezone(UTC).strftime(fmt),
        (now + timedelta(seconds=1)).astimezone(UTC).strftime(fmt),
    )
    for row in reversed(rows):
        if row["kind"] == "game_stop":
            return None
        if row["kind"] == "game_start":
            try:
                data = json.loads(row["data_json"] or "{}")
            except json.JSONDecodeError:
                data = {}
            return _game_name(data if isinstance(data, dict) else {})[:60] or "a game"
    return None


def build_prompt(title: str, minutes: int, location: str | None, game: str | None) -> str:
    where = f" at {location}" if location else ""
    context = f" He's in the middle of {game} right now." if game else ""
    return (
        f'Calendar heads-up: Mario has "{title}"{where} in {minutes} minutes.{context} '
        "Give him a short, natural heads-up in your own voice - one sentence, say what "
        "it is and when. Don't invent details."
    )


async def phrase(
    config: FeltConfig, title: str, minutes: int, location: str | None, game: str | None
) -> str:
    from felt_core.brain.replies import local_reply

    try:
        line = await local_reply(
            [{"role": "user", "content": build_prompt(title, minutes, location, game)}],
            config,
            include_facts=False,
        )
    except local_llm.LocalLLMUnavailable:
        logger.info("calendar heads-up: felt-llm unavailable, using the plain line")
        return fallback_line(title, minutes)
    line = line.strip(" \n\"'")
    return line or fallback_line(title, minutes)


async def deliver(
    config: FeltConfig, calendar_id: str, event: dict, stage: Stage, now: datetime | None = None
) -> bool:
    """Re-check the event, then phrase and enqueue one heads-up. Returns
    whether it was enqueued."""
    key = dedupe_key(event, stage)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            fresh = await google_calendar.get_event(client, config, calendar_id, event["id"])
        now = now or datetime.now(UTC)
        if fresh is None or not is_relevant(fresh):
            logger.info("calendar heads-up: %s deleted/cancelled, dropping", event.get("id"))
            return False
        start = event_start(fresh)
        if start != event_start(event):
            logger.info(
                "calendar heads-up: %s moved, the next poll reschedules it", event.get("id")
            )
            return False
        assert start is not None
        if due_stage(start, now) is not stage or already_sent(config, key):
            return False
        db.set_setting(config.db_path, key, now.isoformat())
        minutes = minutes_left(start, now)
        title = str(fresh.get("summary") or "something on your calendar")
        line = await phrase(
            config, title, minutes, fresh.get("location"), current_game(config, now)
        )
        ttl = (start - now).total_seconds()
        if stage is NORMAL:
            ttl = min(ttl, _NORMAL_TTL_S)
        enqueue_proactive(config, stage.kind, line, ttl_s=ttl)
        logger.info(
            "calendar heads-up: %s-min for %r enqueued as %s", stage.name, title, stage.kind
        )
        return True
    finally:
        _pending.discard(key)


async def _deliver_later(
    config: FeltConfig, delay_s: float, calendar_id: str, event: dict, stage: Stage
) -> None:
    await asyncio.sleep(delay_s)
    try:
        await deliver(config, calendar_id, event, stage)
    except Exception:
        logger.exception("calendar heads-up: delivery failed for %s", event.get("id"))


async def fetch_upcoming(config: FeltConfig, now: datetime) -> list[tuple[str, dict]]:
    async with httpx.AsyncClient(timeout=15) as client:
        calendars = await google_calendar.list_calendars(client, config)
        out: list[tuple[str, dict]] = []
        for cal in calendars:
            for event in await google_calendar.list_events(
                client, config, cal["id"], now, now + LOOKAHEAD
            ):
                out.append((cal["id"], event))
    return out


async def poll_once(config: FeltConfig, now: datetime | None = None) -> int:
    """One poll: fetch the next 2 h and schedule what's due before the
    next poll. Returns how many heads-ups it scheduled."""
    now = now or datetime.now(UTC)
    events = await fetch_upcoming(config, now)
    scheduled = plan(events, now, POLL_INTERVAL_S + 30, lambda key: already_sent(config, key))
    for delay, calendar_id, event, stage in scheduled:
        _pending.add(dedupe_key(event, stage))
        task = asyncio.create_task(_deliver_later(config, delay, calendar_id, event, stage))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return len(scheduled)


async def calendar_headsup_loop(config: FeltConfig) -> None:
    if not config.google_calendar_refresh_token:
        logger.info("calendar heads-up: Google Calendar not connected, loop off")
        return
    while True:
        try:
            await poll_once(config)
        except Exception:
            logger.exception("calendar heads-up: poll failed")
        await asyncio.sleep(POLL_INTERVAL_S)
