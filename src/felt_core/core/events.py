"""Spoken reactions to desktop events (POST /v1/react/event).

felt-tray / felt-animation detect things happening on the desktop - a new
Spotify track (WP-16), a game starting or stopping, Mario coming back from
AFK, the GPU running hot, someone joining the call (WP-17) - and ask
felt-core for a short spoken line about it. Before this, the new-track
remark was smuggled through /v1/context/screen as a fake "Now playing: X"
screen observation; events are NOT screen observations and never touch
that table or the screen-reaction cooldown.

Whether she speaks is decided deterministically, before any model call:
an unknown event, a per-event cooldown, a same-thing dedupe window, a
global minimum gap between remarks, or (for game_stop/afk_end) too short a
duration each mean "no reaction". The model only ever phrases a line that
has already been decided on - it never chooses silence.

The prompt is composed here from a whitelist of typed detail fields, never
by dumping `details`. Lines come from felt-llm only (WP-36 D''): when it is
unreachable LocalLLMUnavailableError propagates and /v1/react/event answers 503.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx2
from pydantic import BaseModel, ConfigDict

from felt_core.clients import local_llm
from felt_core.config import FeltConfig
from felt_core.core import metrics
from felt_core.core.asides import schedule_pick
from felt_core.core.event_policy import decide, dedupe_key, policies
from felt_core.core.event_prompt import UNHUSHED_MAX_WORDS, _text, build_prompt
from felt_core.db.event_reactions import (
    last_event_reaction_line,
    save_event_reaction,
    seconds_since_last_event_reaction,
)
from felt_core.db.kinds import EVENT
from felt_core.db.messages import get_her_recent_lines, save_message
from felt_core.db.observations import seconds_since_last_spoken_reaction

logger = logging.getLogger(__name__)


class EventReaction(BaseModel):
    """`line` empty = no reaction.

    `skipped` then says why (cooldown, unknown_event, ...).
    """

    model_config = ConfigDict(frozen=True)

    line: str = ""
    emotion: str | None = None
    skipped: str | None = None


def cap_words(line: str, max_words: int) -> str:
    """Pure: keep a line within max_words.

    Its first sentence if that fits, otherwise the first max_words words.
    """
    words = line.split()
    if len(words) <= max_words:
        return line
    cut = min((i for i in (line.find(c) for c in ".!?") if i >= 0), default=-1)
    if cut >= 0 and len(line[: cut + 1].split()) <= max_words:
        return line[: cut + 1].strip()
    return " ".join(words[:max_words]).rstrip(",;:-") + "."


def same_line(a: str | None, b: str | None) -> bool:
    """Pure: the same line, ignoring case and punctuation."""

    def norm(text: str | None) -> str:
        return " ".join("".join(c for c in (text or "").lower() if c.isalnum() or c.isspace()).split())

    return bool(norm(a)) and norm(a) == norm(b)


async def _lyric_for(details: dict, config: FeltConfig) -> str | None:
    """Best-effort quotable lyric for a new track.

    The current line if one is active, else the chorus line. Only when
    Spotify is actually playing that same title (so the id/duration LRCLIB
    needs are real).
    """
    if not config.spotify_refresh_token:
        return None
    from felt_core.clients.lyrics_client import current_line, find_chorus, get_lyrics
    from felt_core.clients.spotify_client import get_currently_playing

    title = _text(details, "title").lower()
    try:
        async with httpx2.AsyncClient(timeout=5) as client:
            now_playing = await get_currently_playing(client, config)
            if now_playing is None or now_playing["track"].get("name", "").lower() != title:
                return None
            track = now_playing["track"]
            lyrics = await get_lyrics(
                client,
                Path(config.lyrics_cache_dir),
                track.get("id"),
                track.get("name", ""),
                ", ".join(a["name"] for a in track.get("artists", [])),
                track.get("album", {}).get("name", ""),
                now_playing["duration_ms"] // 1000,
                user_agent=config.lyrics_user_agent,
            )
    except Exception:
        logger.info("event reaction: no lyric for %r (lookup failed)", title, exc_info=True)
        return None
    if not lyrics or not lyrics["synced"]:
        return None
    line = current_line(lyrics["synced"], now_playing["progress_ms"])
    if line:
        return line
    chorus = find_chorus(lyrics["synced"])
    return chorus["text"] if chorus else None


async def _poe_item_details(details: dict, config: FeltConfig) -> dict:
    """The forwarded item text -> the whitelisted fields build_prompt reads.

    Priced (uniques and currency) with poe_price_check.
    """
    from felt_core.brain import poe_reactions

    # Raw, not _text(): that collapses whitespace, and the item text's lines matter.
    item = poe_reactions.parse_item_text(str(details.get("text") or "")[:6000])
    if item is None:
        logger.info("event reaction poe_item: not a PoE item text")
        return {}
    prices = await poe_reactions.price_items([item], config)
    logger.info("poe item: %s (%s) -> %s", item["name"], item["rarity"], prices or "no price")
    return {**item, "prices": prices}


# Mario 2026-10-26 ("it should be more varied"): she picks the feeling of
# her own line, like a screen reaction does - the policy's fixed emotion is
# only the fallback when her reply doesn't carry one. A bracket tag, not a
# JSON schema: llama.cpp's grammar made these lines ~4x slower (2.5 -> 9.4 s).
_EMOTION_TAG = re.compile(r"^\s*\[(\w+)\]\s*")


def _emotion_note(emotions: frozenset[str]) -> str:
    return (
        "\n\nStart with how you feel saying it, in square brackets - one of "
        + ", ".join(sorted(emotions))
        + ' - then the remark, e.g. "[amused] this one again?"'
    )


def _line_and_emotion(text: str, emotions: frozenset[str]) -> tuple[str, str | None]:
    """Her "[feeling] line" reply.

    Without a known tag it's all line, with no emotion of her own.
    """
    tag = _EMOTION_TAG.match(text)
    emotion = tag.group(1).lower() if tag else None
    line = text[tag.end() :] if tag else text
    return line.strip(" \n\"'"), emotion if emotion in emotions else None


async def _generate_line(event: str, prompt: str, config: FeltConfig, tone: str | None) -> tuple[str, str | None]:
    # WP-40: brain.replies is felt-core's small public surface for reply
    # generation - this used to reach straight into brain.py's
    # underscore-prefixed (private-by-convention) internals instead.
    from felt_core.brain.replies import EMOTIONS, local_reply, music_taste_context

    # Music taste keeps her opinions of a track consistent ("oh, this one
    # again?").
    taste = await music_taste_context(config) if event == "music_new_track" else []

    async with metrics.timed("event_reaction", route="local", model=config.local_llm_model):
        # No facts (an event remark doesn't need them); local_reply adds
        # the personality prompt and the length cap.
        text = await local_reply(
            [*taste, {"role": "user", "content": prompt + _emotion_note(EMOTIONS)}],
            config,
            include_facts=False,
            tone=tone,
        )
    return _line_and_emotion(text, EMOTIONS)


async def _skip_reason(
    event: str, details: dict, table: dict, key: str | None, config: FeltConfig, session_id: str
) -> str | None:
    """Why this event gets no line right now (cooldowns, policy), or None."""
    since_screen = await seconds_since_last_spoken_reaction(config.db_path, session_id)
    since_any_event = await seconds_since_last_event_reaction(config.db_path)
    since_any = min((s for s in (since_screen, since_any_event) if s is not None), default=None)
    return decide(
        event,
        details,
        table,
        min_gap_s=config.event_reaction_min_gap_s,
        since_any_remark=since_any,
        since_event=await seconds_since_last_event_reaction(config.db_path, event) if event in table else None,
        since_same=await seconds_since_last_event_reaction(config.db_path, event, key) if key else None,
    )


async def _unhushed_line(
    prompt: str, config: FeltConfig, tone: str | None, previous: str | None
) -> tuple[str, str | None]:
    """The back-from-hush line, capped, asked for once more if it repeats the last one."""
    line, emotion = await _generate_line("unhushed", prompt, config, tone)
    line = cap_words(line, UNHUSHED_MAX_WORDS)
    if same_line(line, previous):
        logger.info("event reaction %s: repeated the last line, asking once more", "unhushed")
        line, emotion = await _generate_line("unhushed", prompt, config, tone)
        line = cap_words(line, UNHUSHED_MAX_WORDS)
    return line, emotion


async def react_to_event(
    event: str,
    details: dict | None,
    config: FeltConfig,
    session_id: str = "dashboard",
    tone: str | None = None,
) -> EventReaction:
    """One desktop event: her spoken line about it, or why there's no reaction."""
    details = details if isinstance(details, dict) else {}
    table = policies(config)
    key = dedupe_key(event, details)

    reason = await _skip_reason(event, details, table, key, config, session_id)
    if reason is not None:
        logger.info("event reaction %s: no line (%s)", event, reason)
        return EventReaction(skipped=reason)

    lyric = _text(details, "lyric") or None
    if event == "music_new_track" and lyric is None:
        lyric = await _lyric_for(details, config)
    previous = await last_event_reaction_line(config.db_path, event) if event == "unhushed" else None
    if event == "poe_item":
        details = await _poe_item_details(details, config)
        if not details.get("prices"):
            logger.info("event reaction poe_item: no line (unpriced)")
            return EventReaction(skipped="unpriced")
    prompt = build_prompt(event, details, lyric, previous)
    if prompt is None:
        logger.info("event reaction %s: details too thin to say anything (%s)", event, sorted(details))
        return EventReaction(skipped="missing_details")
    # Mario 10-03: she knows what she already said on her own, so a song
    # line doesn't echo the remark she made a minute ago
    from felt_core.brain.screen_reactions import RECENT_LINES_N, recent_lines_block

    prompt += recent_lines_block(
        [r["content"] for r in await get_her_recent_lines(config.db_path, session_id, RECENT_LINES_N)]
    )

    try:
        if event == "unhushed":
            line, emotion = await _unhushed_line(prompt, config, tone, previous)
        else:
            line, emotion = await _generate_line(event, prompt, config, tone)
    except local_llm.LocalLLMUnavailableError:
        logger.warning("event reaction %s: felt-llm unavailable", event)
        raise
    except Exception:
        logger.exception("event reaction %s: line generation failed", event)
        return EventReaction(skipped="generation_failed")
    if not line or line.upper() == "NONE" or (event == "unhushed" and same_line(line, previous)):
        return EventReaction(skipped="generation_failed")

    # Cooldown state + chat history (her line shows in the chat window,
    # like a screen remark does) - but never a screen observation.
    await save_event_reaction(config.db_path, session_id, event, key, line)
    await save_message(config.db_path, session_id, "assistant", line, kind=EVENT, source_ref=f"event:{event}")
    await schedule_pick(config, session_id)  # WP-42.2: which asides wait in Talk
    logger.info("event reaction %s (%s): %r", event, emotion or "policy", line)
    return EventReaction(line=line, emotion=emotion or table[event].emotion)
