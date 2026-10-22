"""The morning brief (WP-45, cm-axv; Mario 2026-10-22: "lets get the news going").

Once a day, on the first sign of him after 05:00 (the same day start as the
diary, diary.py), she tells him - desk and phone - what's on today: the
calendar and reminders, the weather, the news he follows (news_digest:
Portugal and Porto first, the world only if something big), and the server
alerts that are still open plus how many came and went overnight. Email
isn't in it (not set up yet). Each part comes from her own skill or the
incidents table; a part that fails or has nothing is left out.

Her diary's one-line morning greeting is folded in as the opening while the
brief is on (two morning messages a minute apart otherwise); it goes out on
its own only if the brief can't run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from felt_core import db, features, llm_gate, local_llm, phone
from felt_core.config import FeltConfig
from felt_core.incidents import _connect, _ensure, _ts
from felt_core.plugin import ExecutionContext
from felt_core.plugin_loader import load_plugins
from felt_core.proactive import enqueue_proactive
from felt_core.tools import run_skill

logger = logging.getLogger(__name__)

FEATURE = "brief.morning"
LISBON = ZoneInfo("Europe/Lisbon")
_DONE_KEY = "brief_last_date"
GREETING_KEY = "brief_greeting"
_START_HOUR = 5
_TTL_S = 3 * 60 * 60
_TIMEOUT_S = 90
# e2e 2026-10-22: a looser ask left the news out of one brief in two
ASK = (
    "Give Mario his morning brief in your own voice: a few short spoken sentences, about 20 "
    "seconds, in English. Cover every part above that has something, in this order: today, "
    "the weather, the news (Portugal and Porto, one line each; the world only if it's big), "
    "the server only if something is still wrong. Don't invent details; keep names and "
    "numbers exactly as they are."
)
# skill, params, the label her facts get
PARTS = (
    ("list_calendar_events", {"day": "today"}, "Today's calendar and reminders"),
    ("get_weather", {"day": "today"}, "Weather today"),
    ("news_digest", {"topic": "all"}, "News"),
)


def due(now: datetime, last: str | None) -> str | None:
    """Pure: today's local date if the brief is due now (after 05:00, not
    given yet today), else None."""
    local = now.astimezone(LISBON)
    if local.hour < _START_HOUR:
        return None
    today = local.date().isoformat()
    return None if last == today else today


def overnight(db_path: str, now: datetime) -> str:
    """The server since 22:00 last night: what's still wrong, and how many
    came and went. Empty when all was quiet."""
    local = now.astimezone(LISBON)
    since = datetime.combine(local.date() - timedelta(days=1), datetime.min.time(), LISBON)
    since = since.replace(hour=22)
    conn = _connect(db_path)
    try:
        _ensure(conn)
        still = [r[0] for r in conn.execute("SELECT title FROM incidents WHERE state = 'open'")]
        gone = conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE first_at >= ?"
            " AND state IN ('resolved', 'cleared', 'handled')",
            (_ts(since),),
        ).fetchone()[0]
    finally:
        conn.close()
    lines = []
    if still:
        lines.append("Still wrong: " + "; ".join(still))
    if gone:
        lines.append(f"{gone} alert(s) overnight that sorted themselves out or were handled")
    return ". ".join(lines)


def will_open_with(config: FeltConfig, now: datetime | None = None) -> bool:
    """Whether today's brief is still to come - then the diary's greeting
    waits for it instead of going out alone."""
    now = now or datetime.now(UTC)
    last = db.get_setting(config.db_path, _DONE_KEY)
    return features.enabled(config.db_path, FEATURE) and due(now, last) is not None


async def gather(config: FeltConfig, now: datetime) -> str:
    """Her facts for the brief, one block per part that has something."""
    registry = features.enabled_skills(config.db_path, load_plugins(config.plugins_dir))
    ctx = ExecutionContext(config=config, original_message="good morning")
    blocks = []
    for skill, params, label in PARTS:
        if skill not in registry:
            continue
        try:
            result = await run_skill(skill, params, registry, ctx)
        except Exception as e:  # one part down never sinks the brief
            logger.warning("brief: %s failed (%r) - left out", skill, e)
            continue
        text = (result.voice_line or result.text or "").strip()
        if text:
            blocks.append(f"{label}:\n{text}")
    if server := overnight(config.db_path, now):
        blocks.append(f"Server overnight:\n{server}")
    return "\n\n".join(blocks)


def fallback(facts: str) -> str:
    """Pure: when felt-llm can't word it - the facts, plainly."""
    return "Good morning. Here's today:\n" + facts if facts else "Good morning."


async def word(config: FeltConfig, facts: str, greeting: str | None) -> str:
    from felt_core.brain.replies import local_reply

    opening = f'Open with this morning thought of yours: "{greeting}"\n\n' if greeting else ""
    try:
        with llm_gate.timely("brief"):
            line = await local_reply(
                [{"role": "user", "content": f"{opening}{facts}\n\n{ASK}"}],
                config,
                include_facts=False,
            )
    except local_llm.LocalLLMUnavailable:
        line = ""
    return line.strip(" \n\"'") or fallback(facts)


async def maybe_brief(config: FeltConfig, now: datetime | None = None) -> str | None:
    """The day's brief if it's due: gathered, worded, sent to the desk and
    the phone. Returns what she said (None when not due / off)."""
    now = now or datetime.now(UTC)
    today = due(now, db.get_setting(config.db_path, _DONE_KEY))
    if today is None or not features.enabled(config.db_path, FEATURE):
        return None
    db.set_setting(config.db_path, _DONE_KEY, today)  # once, even if it fails below
    greeting = db.get_setting(config.db_path, GREETING_KEY) or None
    db.set_setting(config.db_path, GREETING_KEY, "")
    try:
        facts = await gather(config, now)
        line = await asyncio.wait_for(word(config, facts, greeting), _TIMEOUT_S)
    except Exception:
        logger.exception("brief: failed - the diary greeting goes out alone")
        if greeting:
            enqueue_proactive(config, "diary", greeting, ttl_s=_TTL_S)
        return None
    enqueue_proactive(config, "brief", line, ttl_s=_TTL_S)
    try:
        await phone.notify(config, "Good morning", line, tag="felt-brief")
    except phone.PhoneUnavailable:
        logger.info("brief: phone unreachable, desk only")
    logger.info("brief: said %r", line)
    return line
