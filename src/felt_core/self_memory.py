"""Personality plan P4, merged into WP-26 (Mario, 2026-09-27): what Felt
takes away from a day - opinions, likes/dislikes, running jokes, grudges -
distinct from the diary (`felt_core.diary`), which is *their* shared
record of the day. Self-memories are added automatically by the diary's
nightly writer, with no approval queue (Decision 3, 2026-09-27); Mario
views/pins/edits/vetoes/deletes them afterward via /v1/self-memory.

The hard constraint is the prompt budget (Decision 3): at most 8 memories
in any one prompt, keyword-relevant ones first, and the diary snippet plus
memory block together capped at ~200 tokens on top of the persona sheet.
`memory_block` is that budget, injected by `brain.personality`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from felt_core import db
from felt_core.config import FeltConfig

logger = logging.getLogger(__name__)

# Shared with felt_core.diary (Mario's local timezone - felt-core's own
# container runs UTC, same convention as google_calendar.py/
# poe_trade_cache.py's own Europe/Lisbon ZoneInfo).
LISBON = ZoneInfo("Europe/Lisbon")

MAX_ACTIVE = 30
MAX_IN_PROMPT = 8
# ~4 chars/token (cloud.py's own estimate convention) - the diary snippet
# plus the self-memory block together, on top of the persona sheet
# (Decision 3: "diary snippet + memories <= ~200 tokens").
MAX_BLOCK_CHARS = 200 * 4
# A memory used in the last half hour is skipped when ranking, so the
# same one isn't in every single prompt in a row - unless skipping it
# would leave nothing to show at all.
COOLDOWN_S = 1800
# Two memories are "the same idea" if they share this much of their word
# set - a cheap Jaccard check, not an embedding model, good enough to stop
# the writer silently re-adding "bread is the best food" every night.
_DEDUPE_SIMILARITY = 0.6


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def _similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def is_duplicate(text: str, existing_texts: list[str]) -> bool:
    """Pure: whether `text` is close enough to something in
    `existing_texts` that adding it again would just be noise."""
    return any(_similarity(text, existing) >= _DEDUPE_SIMILARITY for existing in existing_texts)


def add_self_memory(
    config: FeltConfig, kind: str, text: str, keywords: list[str], source_date: str
) -> int | None:
    """Adds one self-memory the nightly writer proposed - skipped if it's
    a near-duplicate of an existing active one, or matches something Mario
    already vetoed (vetoed text never comes back, WP-26's spec). Returns
    the new row's id, or None if it was skipped. Enforces the <=30 active
    cap afterward by retiring the least-used, oldest, unpinned actives."""
    text = " ".join(text.split()).strip()
    if not text:
        return None
    vetoed = db.get_vetoed_self_memory_texts(config.db_path)
    if is_duplicate(text, vetoed):
        logger.info("self-memory: %r matches a vetoed memory - not adding", text)
        return None
    active_texts = [m["text"] for m in db.get_self_memories(config.db_path, status="active")]
    if is_duplicate(text, active_texts):
        logger.info("self-memory: %r duplicates an existing active one - not adding", text)
        return None

    memory_id = db.insert_self_memory(config.db_path, kind, text, json.dumps(keywords), source_date)
    _enforce_active_cap(config)
    return memory_id


def _enforce_active_cap(config: FeltConfig) -> None:
    active = list(db.get_self_memories(config.db_path, status="active"))
    overflow = len(active) - MAX_ACTIVE
    if overflow <= 0:
        return
    # "the oldest unused ones retire" (WP-26's spec) - pinned memories are
    # never auto-retired, Mario has to unpin first.
    unpinned = [m for m in active if not m["pinned"]]
    # created_at can tie within the same wall-clock second (a burst of
    # inserts, exactly what a nightly writer or this cap's own test does)
    # - id ascending is the tiebreaker so the actually-oldest (lowest id,
    # inserted first) retires first, not whichever the DB's id-DESC read
    # order happened to list first in the tie.
    unpinned.sort(key=lambda m: (m["uses"], m["created_at"], m["id"]))
    to_retire = [m["id"] for m in unpinned[:overflow]]
    db.retire_self_memories(config.db_path, to_retire)
    if to_retire:
        logger.info(
            "self-memory: retired %d memory(ies) over the %d-active cap", len(to_retire), MAX_ACTIVE
        )


def rank_self_memories(
    memories: list[dict[str, Any]],
    context_text: str | None,
    now: datetime,
    cooldown_s: int = COOLDOWN_S,
) -> list[dict[str, Any]]:
    """Pure: pinned first, then keyword overlap with `context_text` (the
    SillyTavern lorebook idea - an opinion only fires when its topic is
    live), then recency. A memory used within `cooldown_s` is excluded
    from the pool unless that would empty it. Caps to MAX_IN_PROMPT."""
    context_words = _words(context_text) if context_text else set()

    def keyword_hits(memory: dict) -> int:
        if not context_words:
            return 0
        keywords = json.loads(memory.get("keywords_json") or "[]")
        return sum(1 for k in keywords if str(k).lower() in context_words)

    def is_cool(memory: dict) -> bool:
        last_used = memory.get("last_used")
        if not last_used:
            return True
        try:
            used_at = datetime.strptime(last_used, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return True
        return (now.astimezone(UTC) - used_at).total_seconds() >= cooldown_s

    pool = [m for m in memories if is_cool(m)] or list(memories)

    recency_order = sorted(
        pool, key=lambda m: m.get("last_used") or m.get("created_at") or "", reverse=True
    )
    recency_rank = {m["id"]: i for i, m in enumerate(recency_order)}

    ranked = sorted(
        pool, key=lambda m: (0 if m.get("pinned") else 1, -keyword_hits(m), recency_rank[m["id"]])
    )
    return ranked[:MAX_IN_PROMPT]


def _pick_callbacks(callbacks: list[dict], max_n: int = 2) -> tuple[list[str], list[dict]]:
    """Pure: from a diary row's callbacks_json ({"text", "uses"} objects),
    picks up to `max_n` with uses < 2 ("each callback is used at most
    twice, then retired" - WP-26), least-used first, and returns (the
    picked texts, the full list with their uses incremented)."""
    eligible = sorted(
        (c for c in callbacks if c.get("uses", 0) < 2), key=lambda c: c.get("uses", 0)
    )
    picked_ids = {id(c) for c in eligible[:max_n]}
    picked_texts = [c["text"] for c in eligible[:max_n]]
    updated = [{**c, "uses": c.get("uses", 0) + 1} if id(c) in picked_ids else c for c in callbacks]
    return picked_texts, updated


MEMORY_FRAMING = (
    "Your own memories, for background only. Let them colour how you talk, but "
    "don't recite them or reuse their wording, and mention one only when it "
    "actually fits what's happening now."
)


def _recently_block(config: FeltConfig, yesterday: str) -> str:
    diary_row = db.get_diary(config.db_path, yesterday)
    parts: list[str] = []
    if diary_row is not None:
        callbacks = json.loads(diary_row["callbacks_json"] or "[]")
        picked, updated = _pick_callbacks(callbacks)
        if updated != callbacks:
            db.update_diary_callbacks(config.db_path, yesterday, json.dumps(updated))
        bits = ([diary_row["mood_word"]] if diary_row["mood_word"] else []) + picked
        if bits:
            parts.append("yesterday: " + ", ".join(bits))
    week_row = db.get_latest_diary_week(config.db_path)
    if week_row is not None and week_row.get("summary"):
        parts.append(f"this week: {week_row['summary']}")
    if not parts:
        return ""
    return "Recently: " + "; ".join(part.rstrip(". ") for part in parts) + "."


def memory_block(
    config: FeltConfig, context_text: str | None = None, now: datetime | None = None
) -> str:
    """The compiled "Recently (from your diary)" + "Who you've become"
    block every reply carries (via brain.personality.personality_prompt),
    hard-capped at MAX_BLOCK_CHARS (~200 tokens). Each self-memory placed
    in the prompt has its use count/last_used bumped (db.touch_self_memories)
    - what the next call's cooldown/recency ranking is computed from."""
    now = now or datetime.now(UTC)
    yesterday = ((now.astimezone(LISBON)).date() - timedelta(days=1)).isoformat()
    recently = _recently_block(config, yesterday)

    active = db.get_self_memories(config.db_path, status="active")
    ranked = rank_self_memories(active, context_text, now)
    memories_block = ""
    if ranked:
        db.touch_self_memories(config.db_path, [m["id"] for m in ranked])
        memories_block = "Who you've become:\n" + "\n".join(f"- {m['text']}" for m in ranked)

    block = "\n\n".join(s for s in (recently, memories_block) if s)
    if len(block) > MAX_BLOCK_CHARS:
        block = block[:MAX_BLOCK_CHARS].rstrip()
    if block:
        # Every model tried in the WP-36 blind test (cloud included) recited
        # the diary word for word when it was presented as bare text.
        block = f"{MEMORY_FRAMING}\n\n{block}"
        logger.debug("memory block (%d chars, ~%d tokens): %s", len(block), len(block) // 4, block)
    return block
