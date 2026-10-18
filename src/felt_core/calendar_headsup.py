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

Reminders Mario asked for (set_reminder, WP-46, tagged REMINDER_TAG) get
one urgent line right on time instead - "call the dentist" at 3:00, not a
heads-up at 2:30.

Events with an address get leave-by nudges instead (leave_by.py, 2026-10-18):
"leave in 15" and "time to go", from the drive time HA's Waze gives - checked
each poll and again right before each nudge, so traffic moves them - at the
desk and on the phone. If the drive time can't be had, the plain heads-ups.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from felt_core import db, google_calendar, leave_by, local_llm, phone
from felt_core.config import FeltConfig
from felt_core.events import _game_name
from felt_core.proactive import enqueue_proactive

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 5 * 60
# long enough for a drive: an event with an address 3 h away may need leaving now
LOOKAHEAD = timedelta(hours=4)
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
# set_reminder's events: one line at the time itself
REMINDER = Stage("0", 0, "urgent")
REMINDER_TAG = "felt_reminder"
# a reminder still counts this long after its time (a slow poll, a restart)
_REMINDER_GRACE_S = 5 * 60
_REMINDER_TTL_S = 10 * 60
# leave-by (lead_s unused - their time comes from the drive)
LEAVE_SOON = Stage("leave15", 0, "urgent")
LEAVE_NOW = Stage("leave", 0, "urgent")
LEAVE_STAGES = (LEAVE_SOON, LEAVE_NOW)
_LEAVE_TTL_S = 15 * 60
# a nudge whose leave time traffic pushed this much later waits for the new time
_LEAVE_SLACK_S = 2 * 60

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


def is_reminder(event: dict) -> bool:
    """Pure: one of set_reminder's events (recurring instances keep the tag)."""
    return event.get("extendedProperties", {}).get("private", {}).get(REMINDER_TAG) == "1"


def stages_for(event: dict, routable: bool = False) -> tuple[Stage, ...]:
    if is_reminder(event):
        return (REMINDER,)
    return LEAVE_STAGES if routable and leave_by.has_address(event) else STAGES


def leave_fire_at(start: datetime, drive_min: float, stage: Stage) -> datetime:
    leave = leave_by.leave_time(start, drive_min)
    return leave - timedelta(minutes=leave_by.SOON_MIN) if stage is LEAVE_SOON else leave


def due_stage(start: datetime, now: datetime, reminder: bool = False) -> Stage | None:
    """Pure: which heads-up is right for `now` - URGENT inside the last
    10 min, NORMAL between 30 and 10 min out, else none. A late first
    sight (8 min out) skips the 30-min one rather than sending both.
    A reminder: REMINDER from a minute before its time to a few after."""
    left = (start - now).total_seconds()
    if reminder:
        return REMINDER if -_REMINDER_GRACE_S <= left <= 60 else None
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
    events: list[tuple[str, dict]],
    now: datetime,
    horizon_s: float,
    sent,
    drive: dict[str, float] | None = None,
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
        minutes = (drive or {}).get(event.get("id", ""))
        for stage in stages_for(event, routable=minutes is not None):
            key = dedupe_key(event, stage)
            if key in _pending or sent(key):
                continue
            if stage in LEAVE_STAGES:
                assert minutes is not None
                fire = leave_fire_at(start, minutes, stage)
                if stage is LEAVE_SOON and fire + timedelta(minutes=leave_by.SOON_MIN) <= now:
                    continue  # already past the leave time - "time to go" covers it
                delay = (fire - now).total_seconds()
                if delay <= horizon_s and (start - now).total_seconds() > 0:
                    out.append((max(0.0, delay), calendar_id, event, stage))
                continue
            delay = (start - timedelta(seconds=stage.lead_s) - now).total_seconds()
            if delay <= horizon_s and (start - now).total_seconds() > 0:
                out.append((max(0.0, delay), calendar_id, event, stage))
    return out


def minutes_left(start: datetime, now: datetime) -> int:
    return max(1, round((start - now).total_seconds() / 60))


def fallback_line(title: str, minutes: int, reminder: bool = False) -> str:
    if reminder:
        return f"reminder: {title}."
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


def build_prompt(
    title: str, minutes: int, location: str | None, game: str | None, reminder: bool = False
) -> str:
    where = f" at {location}" if location else ""
    context = f" He's in the middle of {game} right now." if game else ""
    if reminder:
        return (
            f'Mario asked you to remind him now: "{title}".{context} Remind him in your own '
            "voice - one short sentence, say what it is. Don't invent details."
        )
    return (
        f'Calendar heads-up: Mario has "{title}"{where} in {minutes} minutes.{context} '
        "Give him a short, natural heads-up in your own voice - one sentence, say what "
        "it is and when. Don't invent details."
    )


async def phrase(
    config: FeltConfig,
    title: str,
    minutes: int,
    location: str | None,
    game: str | None,
    reminder: bool = False,
) -> str:
    from felt_core.brain.replies import local_reply

    prompt = build_prompt(title, minutes, location, game, reminder)
    try:
        line = await local_reply([{"role": "user", "content": prompt}], config, include_facts=False)
    except local_llm.LocalLLMUnavailable:
        logger.info("calendar heads-up: felt-llm unavailable, using the plain line")
        return fallback_line(title, minutes, reminder)
    line = line.strip(" \n\"'")
    return line or fallback_line(title, minutes, reminder)


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
        reminder = is_reminder(fresh)
        if due_stage(start, now, reminder) is not stage or already_sent(config, key):
            return False
        db.set_setting(config.db_path, key, now.isoformat())
        minutes = minutes_left(start, now)
        title = str(fresh.get("summary") or "something on your calendar")
        line = await phrase(
            config, title, minutes, fresh.get("location"), current_game(config, now), reminder
        )
        ttl = (start - now).total_seconds()
        if stage is NORMAL:
            ttl = min(ttl, _NORMAL_TTL_S)
        elif stage is REMINDER:
            ttl = _REMINDER_TTL_S
        enqueue_proactive(config, stage.kind, line, ttl_s=ttl)
        logger.info(
            "calendar heads-up: %s-min for %r enqueued as %s", stage.name, title, stage.kind
        )
        return True
    finally:
        _pending.discard(key)


def build_leave_prompt(
    title: str, at: str, drive_min: int, leave_in: int, now: bool, game: str | None
) -> str:
    context = f" He's in the middle of {game} right now." if game else ""
    if now:
        ask = (
            f'It\'s time for Mario to leave for "{title}" at {at} - about {drive_min} minutes '
            "by car with the traffic right now. Tell him to head out"
        )
    else:
        ask = (
            f'Mario has "{title}" at {at}. The drive is about {drive_min} minutes with the '
            f"traffic right now, so he should leave in {leave_in} minutes. Tell him"
        )
    return f"{ask}, in your own voice - one short sentence.{context} Don't invent details."


def fallback_leave_line(title: str, at: str, drive_min: int, leave_in: int, now: bool) -> str:
    if now:
        return f"time to go: {title} at {at}, {drive_min} min drive."
    return f"leave in {leave_in} min for {title} at {at} ({drive_min} min drive)."


async def deliver_leave(
    config: FeltConfig, calendar_id: str, event: dict, stage: Stage, now: datetime | None = None
) -> bool:
    """A leave-by nudge: re-check the event and the traffic, then say it at
    the desk and on the phone. A leave time that traffic pushed later waits
    for the next poll; one pushed earlier goes out at once."""
    key = dedupe_key(event, stage)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            fresh = await google_calendar.get_event(client, config, calendar_id, event["id"])
        now = now or datetime.now(UTC)
        if fresh is None or not is_relevant(fresh) or event_start(fresh) != event_start(event):
            return False
        start = event_start(fresh)
        assert start is not None
        if start <= now or already_sent(config, key):
            return False
        minutes = await leave_by.drive_minutes(config, fresh.get("location") or "", now)
        if minutes is None:
            minutes = event.get("_drive_min")  # the poll's figure, if HA is briefly away
        if minutes is None:
            return False
        if (leave_fire_at(start, minutes, stage) - now).total_seconds() > _LEAVE_SLACK_S:
            logger.info("leave-by: traffic eased for %s, the next poll replans", event.get("id"))
            return False
        leave = leave_by.leave_time(start, minutes)
        if stage is LEAVE_SOON and leave <= now:
            return False  # past the leave time already - "time to go" says it
        db.set_setting(config.db_path, key, now.isoformat())
        title = str(fresh.get("summary") or "your appointment")
        at = start.astimezone(google_calendar.TZ).strftime("%H:%M")
        drive_min = math.ceil(minutes)
        leave_in = max(0, round((leave - now).total_seconds() / 60))
        go = stage is LEAVE_NOW
        from felt_core.brain.replies import local_reply

        try:
            prompt = build_leave_prompt(
                title, at, drive_min, leave_in, go, current_game(config, now)
            )
            line = (
                await local_reply(
                    [{"role": "user", "content": prompt}], config, include_facts=False
                )
            ).strip(" \n\"'")
        except local_llm.LocalLLMUnavailable:
            line = ""
        line = line or fallback_leave_line(title, at, drive_min, leave_in, go)
        enqueue_proactive(
            config, stage.kind, line, ttl_s=min(_LEAVE_TTL_S, (start - now).total_seconds())
        )
        try:
            await phone.notify(config, title, line, tag=f"felt-leave-{event['id']}")
        except phone.PhoneUnavailable:
            logger.info("leave-by: phone unreachable, desk only")
        logger.info("leave-by: %s for %r (drive %d min)", stage.name, title, drive_min)
        return True
    finally:
        _pending.discard(key)


async def _drive_times(
    config: FeltConfig, events: list[tuple[str, dict]], now: datetime
) -> dict[str, float]:
    """Drive minutes per event with an address that still has a leave nudge to send."""
    out: dict[str, float] = {}
    for _, event in events:
        start = event_start(event)
        if (
            not (is_relevant(event) and leave_by.has_address(event))
            or start is None
            or start <= now
        ):
            continue
        if all(already_sent(config, dedupe_key(event, st)) for st in LEAVE_STAGES):
            continue
        minutes = await leave_by.drive_minutes(config, event["location"], now)
        if minutes is not None:
            out[event["id"]] = minutes
    return out


async def _deliver_later(
    config: FeltConfig, delay_s: float, calendar_id: str, event: dict, stage: Stage
) -> None:
    await asyncio.sleep(delay_s)
    try:
        if stage in LEAVE_STAGES:
            await deliver_leave(config, calendar_id, event, stage)
            return
        await deliver(config, calendar_id, event, stage)
    except Exception:
        logger.exception("calendar heads-up: delivery failed for %s", event.get("id"))


def same_event_key(event: dict) -> tuple:
    """Pure: an appointment entered twice (or in two calendars) is one."""
    return (
        (event.get("summary") or "").strip().lower(),
        event.get("start", {}).get("dateTime"),
        (event.get("location") or "").strip().lower(),
    )


async def fetch_upcoming(config: FeltConfig, now: datetime) -> list[tuple[str, dict]]:
    async with httpx.AsyncClient(timeout=15) as client:
        calendars = await google_calendar.list_calendars(client, config)
        out: list[tuple[str, dict]] = []
        seen: set[tuple] = set()
        for cal in calendars:
            for event in await google_calendar.list_events(
                client, config, cal["id"], now, now + LOOKAHEAD
            ):
                # 2026-10-18: Tuesday's appointment was in the calendar twice -
                # every nudge would have come twice, desk and phone
                key = same_event_key(event)
                if key in seen:
                    continue
                seen.add(key)
                out.append((cal["id"], event))
    return out


async def poll_once(config: FeltConfig, now: datetime | None = None) -> int:
    """One poll: fetch the next 4 h and schedule what's due before the
    next poll. Returns how many heads-ups it scheduled."""
    now = now or datetime.now(UTC)
    events = await fetch_upcoming(config, now)
    drive = await _drive_times(config, events, now)
    events = [
        (c, {**e, "_drive_min": drive[e["id"]]} if e.get("id") in drive else e) for c, e in events
    ]
    scheduled = plan(
        events, now, POLL_INTERVAL_S + 30, lambda key: already_sent(config, key), drive
    )
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
