"""WP-26: the diary (day-to-day continuity), combined with personality
plan P4 self-memory (Mario, 2026-09-27: "do it along with the personality
change since she will have memories on that side too"). One nightly
felt-llm call reads one set of material about a day and writes both halves
in a single JSON response: the diary
(their shared record - `entry`/`callbacks`/`mood_word`) and self-memory
(what SHE takes away - `self_updates`, felt_core.core.self_memory), plus one
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
from datetime import UTC, datetime, timedelta

from felt_core.brain.persona import compile_persona
from felt_core.clients import local_llm
from felt_core.config import FeltConfig
from felt_core.core import features, llm_gate
from felt_core.core.diary_material import gather_material
from felt_core.core.proactive import enqueue_proactive
from felt_core.core.self_memory import LISBON, add_self_memory
from felt_core.db.diary import get_diary, insert_diary, insert_or_update_diary_week
from felt_core.db.self_memory import get_self_memories, get_vetoed_self_memory_texts
from felt_core.db.settings import set_setting

logger = logging.getLogger(__name__)

_TRIGGER_KINDS = ("day_start", "afk_end")
_TRIGGER_HOUR = 5
_GREETING_TTL_S = 2 * 60 * 60
_MAX_CALLBACKS = 5
_MAX_SELF_UPDATES = 3
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


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


def _build_writer_prompt(material: str, active_memories: list[dict], vetoed_texts: list[str]) -> str:
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


async def _generate_json(config: FeltConfig, system: str, user_prompt: str, *, max_tokens: int) -> str:
    """One felt-llm call for the nightly writers. Raises LocalLLMUnavailableError
    when it's down; callers log and skip.
    """
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
    day), felt-llm was unavailable, the response didn't parse, or the diary
    is switched off.
    """
    if not features.enabled(config.db_path, "memory.diary"):
        return False
    if get_diary(config.db_path, date_str) is not None:
        return False

    material = await gather_material(config, date_str)
    if not material.strip():
        logger.info("diary: no material for %s (AFK/away all day) - skipping", date_str)
        return False

    active_memories = get_self_memories(config.db_path, status="active")
    vetoed_texts = get_vetoed_self_memory_texts(config.db_path)
    system = compile_persona() + "\n\n" + _WRITING_INSTRUCTIONS
    user_prompt = _build_writer_prompt(material, active_memories, vetoed_texts)

    try:
        raw = await _generate_json(config, system, user_prompt, max_tokens=1200)
    except local_llm.LocalLLMUnavailableError:
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
        {"text": str(c).strip(), "uses": 0} for c in (parsed.get("callbacks") or [])[:_MAX_CALLBACKS] if str(c).strip()
    ]
    mood_word = str(parsed.get("mood_word") or "").strip()
    insert_diary(config.db_path, date_str, entry, json.dumps(callbacks), mood_word)
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
    if greeting and features.enabled(config.db_path, "ambient.morning_greeting"):
        from felt_core.core import brief

        if brief.will_open_with(config):  # the brief opens with it (brief.py)
            set_setting(config.db_path, brief.GREETING_KEY, greeting)
        else:
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
    simple; monthly can be a TODO note").
    """
    end = datetime.strptime(date_str, "%Y-%m-%d").date()
    week_dates = [(end - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    rows = [r for d in week_dates if (r := get_diary(config.db_path, d)) is not None]
    if not rows:
        return

    material = "\n".join(f"{r['date']} ({r['mood_word']}): {r['entry']}" for r in rows)
    system = compile_persona() + "\n\n" + _WEEKLY_INSTRUCTIONS
    try:
        raw = await _generate_json(config, system, material, max_tokens=600)
    except local_llm.LocalLLMUnavailableError:
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
        {"text": str(c).strip(), "uses": 0} for c in (parsed.get("callbacks") or [])[:_MAX_CALLBACKS] if str(c).strip()
    ]
    week = _iso_week(date_str)
    insert_or_update_diary_week(config.db_path, week, summary, json.dumps(callbacks))
    logger.info("diary: compressed week %s from %d entries", week, len(rows))


async def _maybe_compress_week(config: FeltConfig, date_str: str) -> None:
    if datetime.strptime(date_str, "%Y-%m-%d").date().weekday() == 6:  # Sunday
        await _compress_week(config, date_str)


# -- the trigger, called from POST /v1/events --------------------------------


def _target_date_for_trigger(kind: str, at_utc_str: str) -> str | None:
    """Pure: given a logged event's kind and its normalized UTC `at`,
    which date's diary this should try to write - None if this event
    isn't a trigger at all (wrong kind, or before 05:00 local).
    """
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
    (silent, best-effort) event log itself.
    """
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
        with llm_gate.maintenance("diary"):
            await write_diary_for_date(config, target_date)
    except Exception:
        logger.exception("diary: writer failed for %s", target_date)
    finally:
        _writing.discard(target_date)
    await _brief(config)


async def _brief(config: FeltConfig) -> None:
    """The morning brief after the diary (WP-45, brief.py) - its greeting
    is the brief's opening. Once a day; errors logged, never raised.
    """
    from felt_core.core import brief

    try:
        await brief.maybe_brief(config)
    except Exception:
        logger.exception("brief: failed")


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
    """Fire-and-forget day-start trigger: yesterday's diary entry if it's
    missing (not retried within RETRY_S), then the morning brief if it's
    still due today. A no-op before 05:00 local.
    """
    import asyncio

    from felt_core.core import brief

    at = (now or datetime.now(UTC)).astimezone(UTC)
    at_str = at.strftime("%Y-%m-%d %H:%M:%S")
    target = _target_date_for_trigger("day_start", at_str)
    if target is None or target in _writing:
        return
    last = _tried.get(target)
    if last is not None and at.timestamp() - last < RETRY_S:
        # the diary was just tried; the brief (WP-45) may still be due
        if brief.will_open_with(config, at):
            work = _brief(config)
        else:
            return
    else:
        _tried[target] = at.timestamp()
        if get_diary(config.db_path, target) is None:
            work = on_event(config, "day_start", at_str)  # the diary, then the brief
        elif brief.will_open_with(config, at):
            work = _brief(config)
        else:
            return
    task = asyncio.get_running_loop().create_task(work)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
