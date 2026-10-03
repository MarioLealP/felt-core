"""WP-26: the diary (day-to-day continuity), combined with personality
plan P4 self-memory (Mario, 2026-09-27: "do it along with the personality
change since she will have memories on that side too"). One nightly
felt-llm call reads one set of material about a day and writes both halves
in a single JSON response: the diary
(their shared record - `entry`/`callbacks`/`mood_word`) and self-memory
(what SHE takes away - `self_updates`, felt_core.self_memory), plus one
morning greeting line to enqueue via the WP-25 proactive channel.

Trigger: the first `day_start`/`afk_end` event after 05:00 local
(Europe/Lisbon) for a date with no diary row yet writes YESTERDAY's entry
(`on_event`, called from api.py's POST /v1/events inside that request's
own event loop, so proactive.enqueue_proactive's audio pre-synthesis
actually fires - WP-25's implementation note on why that matters).

Privacy: nothing leaves the LAN (felt-llm is the only model, WP-36 D''), so
chat goes in as whole session windows with no classifier gate. Facts are
never read here at all. When felt-llm is unreachable the writer logs at
INFO and skips (no fallback).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from felt_core import db, local_llm
from felt_core.brain.persona import compile_persona
from felt_core.config import FeltConfig
from felt_core.proactive import enqueue_proactive
from felt_core.self_memory import LISBON, add_self_memory

logger = logging.getLogger(__name__)

_TRIGGER_KINDS = ("day_start", "afk_end")
_TRIGGER_HOUR = 5
_GREETING_TTL_S = 2 * 60 * 60
_MAX_MATERIAL_CHARS = 3000
_MAX_CALLBACKS = 5
_MAX_SELF_UPDATES = 3
_MAX_EVENT_DATA_CHARS = 2000
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


# -- pure helpers used by api.py's POST /v1/events --------------------------


def normalize_at(at: str | None) -> str:
    """Pure: whatever felt-animation sent (an ISO8601 string, possibly
    with a 'Z' suffix and no offset - treated as local Europe/Lisbon time
    - or nothing at all) into the UTC 'YYYY-MM-DD HH:MM:SS' string every
    other timestamp column in this DB is stored as, so date-range queries
    stay a plain string comparison."""
    if at:
        try:
            parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LISBON)
            dt = parsed.astimezone(UTC)
        except ValueError:
            dt = datetime.now(UTC)
    else:
        dt = datetime.now(UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def cap_event_data(data: dict) -> str:
    """Pure: `data` json-encoded, or a stand-in marker if it's too big
    ("cap data size" - WP-26's spec) - never a chopped, invalid-JSON
    string."""
    try:
        encoded = json.dumps(data, default=str)
    except (TypeError, ValueError):
        return "{}"
    if len(encoded) > _MAX_EVENT_DATA_CHARS:
        return json.dumps({"_truncated": True, "keys": list(data)[:10]})
    return encoded


# -- material gathering (pure formatting + one async gatherer) --------------


def _text(data: dict, key: str) -> str:
    v = data.get(key)
    return " ".join(str(v).split())[:80] if v is not None else ""


def _num(data: dict, key: str) -> float | None:
    try:
        return float(data[key])
    except (KeyError, TypeError, ValueError):
        return None


def _event_line(kind: str, data: dict) -> str | None:
    """Pure: one compact, cloud-safe line for a logged event, or None for
    a kind with nothing worth saying. Whitelisted fields only - same
    discipline as events.py's build_prompt, never dumps `data` wholesale."""
    if kind == "game_start":
        name = _text(data, "name")
        return f'started playing "{name}"' if name else None
    if kind == "game_stop":
        name = _text(data, "name")
        if not name:
            return None
        dur = _num(data, "duration_s")
        suffix = f" (about {round(dur / 60)} min)" if dur else ""
        return f'stopped playing "{name}"{suffix}'
    if kind == "afk_start":
        return "stepped away from the desk"
    if kind == "afk_end":
        dur = _num(data, "duration_s")
        return f"back after about {round(dur / 60)} min away" if dur else "back at the desk"
    if kind == "day_start":
        return None  # a trigger marker, not something worth narrating
    if kind == "music_track":
        title, artist = _text(data, "title"), _text(data, "artist")
        if not title:
            return None
        return f'played "{title}"' + (f" by {artist}" if artist else "")
    if kind == "mood":
        axes = data.get("axes")
        if isinstance(axes, dict) and axes:
            top = max(axes.items(), key=lambda kv: abs(_as_float(kv[1])), default=None)
            if top:
                return f"mood leaned {top[0]}"
        return None
    if kind == "window_fall":
        cause = _text(data, "cause")
        return f"took a tumble ({cause})" if cause else "took a tumble"
    if kind == "prop_used":
        item = _text(data, "item")
        return f"played with the {item}" if item else None
    if kind == "hush":
        minutes = _num(data, "minutes")
        return f"got hushed for {round(minutes)} min" if minutes else "got hushed"
    if kind == "ledge_sit":
        return "sat on a ledge for a while"
    if kind == "hobby":
        from felt_core.events import HOBBY_PHRASES

        hobby = _text(data, "hobby")
        if hobby not in HOBBY_PHRASES:
            return None
        minutes = _num(data, "minutes")
        spent = f"spent about {round(minutes)} min " if minutes else "spent some time "
        # The event prompt talks TO her ("your handheld"); the diary is hers.
        return spent + HOBBY_PHRASES[hobby].replace("your", "my") + " while Mario was away"
    return None


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class RawMaterial:
    seen: list[dict]
    events: list[dict]
    chat_windows: list[str]


def compact_material(raw: RawMaterial) -> str:
    """Pure: dedupes and caps raw rows into one compact block for the
    writer prompt. Never touches facts - this whole module never reads
    the facts table at all."""
    lines: list[str] = []
    for obs in raw.seen:
        description = obs.get("description") or ""
        if not description:
            continue
        if obs.get("spoken_reply"):
            lines.append(f"saw: {description} - she said: {obs['spoken_reply']}")
        else:
            lines.append(f"saw: {description}")
    for ev in raw.events:
        try:
            data = json.loads(ev["data_json"])
        except json.JSONDecodeError:
            data = {}
        line = _event_line(ev["kind"], data if isinstance(data, dict) else {})
        if line:
            lines.append(line)
    # Dedupe while keeping first-seen order (the standard dict.fromkeys trick).
    lines = list(dict.fromkeys(lines))
    for window in raw.chat_windows:
        lines.append(f"chat: {window}")

    return "\n".join(lines)[:_MAX_MATERIAL_CHARS]


def _lisbon_date_bounds_utc(date_str: str) -> tuple[str, str]:
    """Pure: the [start, end) of one Europe/Lisbon calendar date, as UTC
    'YYYY-MM-DD HH:MM:SS' strings - what every created_at/at column in
    this DB is stored as, so a plain string comparison works."""
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=LISBON)
    start = day_start.astimezone(UTC)
    end = (day_start + timedelta(days=1)).astimezone(UTC)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


async def gather_material(config: FeltConfig, date_str: str) -> str:
    """Material for one Europe/Lisbon calendar date: screen `seen` text
    plus her spoken replies, logged desktop events, and chat as whole
    session windows."""
    start, end = _lisbon_date_bounds_utc(date_str)
    seen = db.get_screen_observations_between(config.db_path, start, end)
    events = db.get_events_between(config.db_path, start, end)
    sessions = db.get_message_sessions_between(config.db_path, start, end)

    chat_windows: list[str] = []
    for session in sessions:
        window_text = "\n".join(f"{m['role']}: {m['content']}" for m in session)
        if not window_text.strip():
            continue
        chat_windows.append(window_text[:500])

    return compact_material(RawMaterial(seen=seen, events=events, chat_windows=chat_windows))


# -- the nightly writer -------------------------------------------------------

_WRITING_INSTRUCTIONS = (
    "You're writing your own diary entry about yesterday, knowing Mario "
    "reads every one. Be readable and cheeky: playful, teasing, "
    "affectionate - his flaws are material, never cruelty. Write in first "
    "person, 120-250 words, about YOUR day together (not a dry log). Then "
    "give up to 5 short callback hooks worth bringing up again later (e.g. "
    "'rage-quit Elden Ring after 3 deaths to the same boss'), one mood "
    "word for the whole day, up to 3 new self_updates (things you decided "
    "about yourself - an opinion, a like, a dislike, a running joke, a "
    "grudge - each with a few keywords that would make it relevant later), "
    "and one short morning greeting line in your own voice that "
    "references yesterday, for the next time Mario shows up.\n\n"
    "Rules: never contradict who you already are (given below); never "
    "duplicate a self-memory you already have; never propose anything on "
    "the never-add list; never invent anything not actually in the "
    "material below.\n\n"
    'Respond with ONLY a JSON object: {"entry": "...", "callbacks": '
    '["...", ...up to 5], "mood_word": "...", "self_updates": '
    '[{"kind": "opinion|like|dislike|running_joke|grudge", "text": "...", '
    '"keywords": ["...", ...]}, ...up to 3], "greeting": "..."}'
)


def _build_writer_prompt(
    material: str, active_memories: list[dict], vetoed_texts: list[str]
) -> str:
    parts = [f"What happened yesterday:\n{material}"]
    if active_memories:
        existing = "\n".join(f"- {m['text']}" for m in active_memories)
        parts.append(f"Self-memories you already have (don't duplicate these):\n{existing}")
    if vetoed_texts:
        never = "\n".join(f"- {t}" for t in vetoed_texts)
        parts.append(f"Never propose anything like these (Mario already rejected them):\n{never}")
    return "\n\n".join(parts)


def _parse_writer_json(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(raw)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


async def _generate_json(
    config: FeltConfig, system: str, user_prompt: str, *, max_tokens: int
) -> str:
    """One felt-llm call for the nightly writers. Raises LocalLLMUnavailable
    when it's down; callers log and skip."""
    reply = await local_llm.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}],
        max_tokens=max_tokens,
        config=config,
    )
    return reply.text


async def write_diary_for_date(config: FeltConfig, date_str: str) -> bool:
    """The nightly writer for one date - ONE felt-llm call producing the
    diary entry, its callbacks/mood, up to 3 new self-memories, and the
    morning greeting all at once. Returns whether an entry was actually
    written - False if there was no material (Mario was AFK/away the whole
    day), felt-llm was unavailable, or the response didn't parse."""
    if db.get_diary(config.db_path, date_str) is not None:
        return False

    material = await gather_material(config, date_str)
    if not material.strip():
        logger.info("diary: no material for %s (AFK/away all day) - skipping", date_str)
        return False

    active_memories = db.get_self_memories(config.db_path, status="active")
    vetoed_texts = db.get_vetoed_self_memory_texts(config.db_path)
    system = compile_persona() + "\n\n" + _WRITING_INSTRUCTIONS
    user_prompt = _build_writer_prompt(material, active_memories, vetoed_texts)

    try:
        raw = await _generate_json(config, system, user_prompt, max_tokens=1200)
    except local_llm.LocalLLMUnavailable:
        logger.info("diary: felt-llm unavailable, skipping the entry for %s", date_str)
        return False

    parsed = _parse_writer_json(raw)
    if parsed is None:
        logger.warning("diary: writer JSON parse failed for %s - skipping", date_str)
        return False

    entry = str(parsed.get("entry") or "").strip()
    if not entry:
        logger.warning("diary: writer returned an empty entry for %s - skipping", date_str)
        return False

    callbacks = [
        {"text": str(c).strip(), "uses": 0}
        for c in (parsed.get("callbacks") or [])[:_MAX_CALLBACKS]
        if str(c).strip()
    ]
    mood_word = str(parsed.get("mood_word") or "").strip()
    db.insert_diary(config.db_path, date_str, entry, json.dumps(callbacks), mood_word)
    logger.info("diary: wrote entry for %s (%d chars, mood=%r)", date_str, len(entry), mood_word)

    for update in (parsed.get("self_updates") or [])[:_MAX_SELF_UPDATES]:
        if not isinstance(update, dict):
            continue
        kind = str(update.get("kind") or "").strip()
        text = str(update.get("text") or "").strip()
        keywords = [str(k).strip() for k in (update.get("keywords") or []) if str(k).strip()]
        if kind and text:
            add_self_memory(config, kind, text, keywords, date_str)

    greeting = str(parsed.get("greeting") or "").strip()
    if greeting:
        enqueue_proactive(config, "diary", greeting, ttl_s=_GREETING_TTL_S)

    await _maybe_compress_week(config, date_str)
    return True


# -- Sunday weekly compression ------------------------------------------------


def _iso_week(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


_WEEKLY_INSTRUCTIONS = (
    "Condense this week's diary entries into one short weekly summary in "
    "your own voice, 2-4 sentences, plus up to 5 callback hooks for the "
    'week. Respond with ONLY JSON: {"summary": "...", "callbacks": '
    '["...", ...]}'
)


async def _compress_week(config: FeltConfig, date_str: str) -> None:
    """The week ending on `date_str` (inclusive, 7 days back) condensed
    into one diary_weeks row - kept simple per WP-26's spec ("keep it
    simple; monthly can be a TODO note")."""
    end = datetime.strptime(date_str, "%Y-%m-%d").date()
    week_dates = [(end - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    rows = [r for d in week_dates if (r := db.get_diary(config.db_path, d)) is not None]
    if not rows:
        return

    material = "\n".join(f"{r['date']} ({r['mood_word']}): {r['entry']}" for r in rows)
    system = compile_persona() + "\n\n" + _WEEKLY_INSTRUCTIONS
    try:
        raw = await _generate_json(config, system, material, max_tokens=600)
    except local_llm.LocalLLMUnavailable:
        logger.info("diary: felt-llm unavailable, skipping weekly compression for %s", date_str)
        return
    parsed = _parse_writer_json(raw)
    if parsed is None:
        logger.warning("diary: weekly compression JSON parse failed for %s", date_str)
        return
    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        return
    callbacks = [
        {"text": str(c).strip(), "uses": 0}
        for c in (parsed.get("callbacks") or [])[:_MAX_CALLBACKS]
        if str(c).strip()
    ]
    week = _iso_week(date_str)
    db.insert_or_update_diary_week(config.db_path, week, summary, json.dumps(callbacks))
    logger.info("diary: compressed week %s from %d entries", week, len(rows))


async def _maybe_compress_week(config: FeltConfig, date_str: str) -> None:
    if datetime.strptime(date_str, "%Y-%m-%d").date().weekday() == 6:  # Sunday
        await _compress_week(config, date_str)


# -- the trigger, called from POST /v1/events --------------------------------


def _target_date_for_trigger(kind: str, at_utc_str: str) -> str | None:
    """Pure: given a logged event's kind and its normalized UTC `at`,
    which date's diary this should try to write - None if this event
    isn't a trigger at all (wrong kind, or before 05:00 local)."""
    if kind not in _TRIGGER_KINDS:
        return None
    at_utc = datetime.strptime(at_utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    local = at_utc.astimezone(LISBON)
    if local.hour < _TRIGGER_HOUR:
        return None
    return (local.date() - timedelta(days=1)).isoformat()


async def on_event(config: FeltConfig, kind: str, at_utc_str: str) -> None:
    """Called from api.py's POST /v1/events for every logged event -
    checks the WP-26 trigger (the first day_start/afk_end after 05:00
    local for a date with no diary row yet) and writes that date's entry
    if so. A no-op for every other event kind or time of day. Errors are
    logged, never raised - a diary-writer failure must never break the
    (silent, best-effort) event log itself."""
    target_date = _target_date_for_trigger(kind, at_utc_str)
    if target_date is None:
        return
    # The endpoint runs this as a background task after replying 204, so
    # felt-animation's 2 s post timeout never fires on the several-second
    # write and re-sends day_start. This guard also covers a genuine
    # duplicate (a buffered retry, an afk_end right after day_start):
    # without it, a second write could start before the first stored its row,
    # meaning two model calls and two morning greetings.
    if target_date in _writing:
        return
    _writing.add(target_date)
    try:
        await write_diary_for_date(config, target_date)
    except Exception:
        logger.exception("diary: writer failed for %s", target_date)
    finally:
        _writing.discard(target_date)


_writing: set[str] = set()


# -- any sign of him ---------------------------------------------------------

# Mario 10-03: she lives on the server, so the day starts with any sign of
# him (a message from any device, an app window opening), not only the
# desktop character's day_start. Each date is tried at most once per
# RETRY_S, so a busy morning of messages doesn't re-gather material on every
# line when there's nothing to write.
RETRY_S = 15 * 60
_tried: dict[str, float] = {}
_tasks: set = set()


def he_is_here(config: FeltConfig, now: datetime | None = None) -> None:
    """Fire-and-forget day-start trigger; a no-op before 05:00 local, once
    yesterday's entry exists, or when that date was tried recently."""
    import asyncio

    at = (now or datetime.now(UTC)).astimezone(UTC)
    at_str = at.strftime("%Y-%m-%d %H:%M:%S")
    target = _target_date_for_trigger("day_start", at_str)
    if target is None or target in _writing:
        return
    last = _tried.get(target)
    if last is not None and at.timestamp() - last < RETRY_S:
        return
    _tried[target] = at.timestamp()
    if db.get_diary(config.db_path, target) is not None:
        return
    task = asyncio.get_running_loop().create_task(on_event(config, "day_start", at_str))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
