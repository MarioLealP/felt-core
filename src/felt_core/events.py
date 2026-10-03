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
unreachable LocalLLMUnavailable propagates and /v1/react/event answers 503.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from felt_core import local_llm, metrics
from felt_core.asides import schedule_pick
from felt_core.config import FeltConfig
from felt_core.db import (
    last_event_reaction_line,
    save_event_reaction,
    save_message,
    seconds_since_last_event_reaction,
    seconds_since_last_spoken_reaction,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventPolicy:
    # Minimum seconds between two spoken lines for this event type.
    cooldown_s: int
    # Minimum seconds before the SAME thing (same track, same game) gets a
    # line again, even if the event cooldown has passed. 0 = no dedupe.
    dedupe_s: int = 0
    # For events that carry details.duration_s: below this, no line.
    min_duration_s: int = 0
    # X-Emotion sent with the line (the WP-07 fixed set).
    emotion: str = "neutral"
    # Mario-triggered events (unhushed) speak even if she just said
    # something: the global min gap is for her unprompted chatter.
    bypass_min_gap: bool = False


# WP-17's table: game_stop -> "finally done?", afk_end -> a greeting,
# spoken only after a long absence; game_start (comfy pose), gpu_hot (a
# cool-off drink from her water bottle) and call_join_guess (a small wave) are mainly non-verbal on the
# desktop side, so their lines are kept rare (long cooldowns). Override
# any field per deployment with FELT_EVENT_POLICIES (JSON).
DEFAULT_POLICIES: dict[str, EventPolicy] = {
    "music_new_track": EventPolicy(cooldown_s=5 * 60, dedupe_s=60 * 60, emotion="happy"),
    "game_start": EventPolicy(cooldown_s=20 * 60, dedupe_s=2 * 60 * 60, emotion="excited"),
    "game_stop": EventPolicy(cooldown_s=20 * 60, min_duration_s=20 * 60, emotion="amused"),
    "afk_end": EventPolicy(cooldown_s=30 * 60, min_duration_s=30 * 60, emotion="happy"),
    "gpu_hot": EventPolicy(cooldown_s=60 * 60, emotion="surprised"),
    "call_join_guess": EventPolicy(cooldown_s=20 * 60, emotion="happy"),
    # WP-29 v2: Mario pressed Exiled Exchange 2's price-check key on an item.
    # He asked, so no min gap; a few seconds against a double press only.
    "poe_item": EventPolicy(cooldown_s=4, emotion="excited", bypass_min_gap=True),
    # Mario lifting a hush (felt-animation /unhush?announce=1, 2026-09-26:
    # "give her some snappy comments about being unmuted"). He triggered
    # it, so only a small anti-double-press cooldown and no min gap.
    "unhushed": EventPolicy(cooldown_s=20, emotion="amused", bypass_min_gap=True),
}

_MAX_FIELD_CHARS = 120

_EVENT_INSTRUCTION = (
    "Say ONE short, natural spoken remark about it (max ~15 words), in character. "
    "React like a friend in the room. No questions that need an answer."
)


@dataclass(frozen=True)
class EventReaction:
    """`line` empty = no reaction; `skipped` then says why (cooldown,
    unknown_event, ...)."""

    line: str = ""
    emotion: str | None = None
    skipped: str | None = None


def policies(config: FeltConfig) -> dict[str, EventPolicy]:
    table = dict(DEFAULT_POLICIES)
    if config.event_policies:
        try:
            override = json.loads(config.event_policies)
        except json.JSONDecodeError:
            logger.warning("FELT_EVENT_POLICIES isn't valid JSON - ignoring it")
            override = {}
        for event, fields in override.items():
            if event in table and isinstance(fields, dict):
                known = {k: v for k, v in fields.items() if k in EventPolicy.__dataclass_fields__}
                table[event] = EventPolicy(**{**table[event].__dict__, **known})
    return table


def _text(details: dict, key: str) -> str:
    value = details.get(key)
    if value is None:
        return ""
    return " ".join(str(value).split())[:_MAX_FIELD_CHARS]


def _number(details: dict, key: str) -> float | None:
    try:
        return float(details[key])
    except (KeyError, TypeError, ValueError):
        return None


def _game_name(details: dict) -> str:
    name = _text(details, "name") or _text(details, "game")
    return name[:-4] if name.lower().endswith(".exe") else name


# WP-30: felt-animation's hobby keys -> how she'd say them. Whitelisted like
# every other detail here - an unknown key is simply not mentioned.
HOBBY_PHRASES = {
    "reading": "reading a book",
    "gaming": "playing on your handheld",
    "napping": "napping",
    "tidying": "tidying up your stuff",
    "stretching": "stretching and looking around",
}


def _hobbies_phrase(details: dict) -> str:
    """ "reading a book (about 12 min) and napping" from details["hobbies"]."""
    parts = []
    for h in details.get("hobbies") or []:
        if not isinstance(h, dict) or h.get("hobby") not in HOBBY_PHRASES:
            continue
        mins = _number(h, "minutes")
        parts.append(HOBBY_PHRASES[h["hobby"]] + (f" (about {round(mins)} min)" if mins else ""))
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0] if parts else ""


def _spoken_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"about {max(minutes, 1)} minutes"
    hours = round(seconds / 3600 * 2) / 2
    return f"about {hours:g} hours"


def dedupe_key(event: str, details: dict) -> str | None:
    """Pure: what counts as "the same thing" for an event's dedupe window."""
    if event == "music_new_track":
        key = f"{_text(details, 'title').lower()}|{_text(details, 'artist').lower()}"
        return key if key != "|" else None
    if event in ("game_start", "game_stop"):
        return _game_name(details).lower() or None
    return None


def decide(
    event: str,
    details: dict,
    table: dict[str, EventPolicy],
    *,
    min_gap_s: int,
    since_any_remark: float | None,
    since_event: float | None,
    since_same: float | None,
) -> str | None:
    """Pure: None = speak, otherwise the reason not to. The `since_*` ages
    are seconds since the last spoken remark of any kind / of this event /
    about this same thing (None = never)."""
    policy = table.get(event)
    if policy is None:
        return "unknown_event"
    if policy.min_duration_s:
        duration = _number(details, "duration_s")
        if duration is None or duration < policy.min_duration_s:
            return "too_short"
    if not policy.bypass_min_gap and since_any_remark is not None and since_any_remark < min_gap_s:
        return "min_gap"
    if since_event is not None and since_event < policy.cooldown_s:
        return "cooldown"
    if policy.dedupe_s and since_same is not None and since_same < policy.dedupe_s:
        return "duplicate"
    return None


UNHUSHED_MAX_WORDS = 12


def _unhushed_prompt(details: dict, previous: str | None) -> str:
    hushed_for = _number(details, "hushed_for_s") or 0
    planned = _number(details, "planned_s") or 0
    early = details.get("early") is True
    if early and planned:
        lasted = "a few seconds" if hushed_for < 60 else _spoken_duration(hushed_for)
        situation = (
            f"Mario hushed you for {_spoken_duration(planned)} but let you talk again after only "
            f"{lasted}. Tease him that he couldn't last without you."
        )
    elif planned:
        situation = (
            f"Mario hushed you and you stayed quiet the whole {_spoken_duration(planned)}. "
            "Be dramatic about how hard it was to stay quiet that long."
        )
    else:
        situation = "Mario just let you talk again after hushing you. Tease him about it."
    avoid = (
        f' Your last line when unhushed was "{previous}" - say something different.'
        if previous
        else ""
    )
    return (
        f"{situation}{avoid} Say ONE short, snappy, teasing line (max {UNHUSHED_MAX_WORDS} words), "
        "in character. No questions that need an answer. Plain spoken words only: no *actions*, "
        "stage directions, sound words like *yawn*, or emoji."
    )


def cap_words(line: str, max_words: int) -> str:
    """Pure: keep a line within max_words - its first sentence if that
    fits, otherwise the first max_words words."""
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
        return " ".join(
            "".join(c for c in (text or "").lower() if c.isalnum() or c.isspace()).split()
        )

    return bool(norm(a)) and norm(a) == norm(b)


def build_prompt(
    event: str, details: dict, lyric: str | None = None, previous: str | None = None
) -> str | None:
    """Pure: the user-turn text for an event, from whitelisted fields
    only. None if the details are too thin to say anything real about.
    `previous` is this event's last spoken line (unhushed uses it so the
    same line never plays twice in a row)."""
    if event == "unhushed":
        return _unhushed_prompt(details, previous)
    if event == "music_new_track":
        title, artist, album = (
            _text(details, "title"),
            _text(details, "artist"),
            _text(details, "album"),
        )
        if not title:
            return None
        song = (
            f'"{title}"'
            + (f" by {artist}" if artist else "")
            + (f" (from {album})" if album else "")
        )
        lyric_part = f' A line from it: "{lyric}". You may quote it if it fits.' if lyric else ""
        return f"Mario just started playing {song}.{lyric_part} {_EVENT_INSTRUCTION}"
    if event == "game_start":
        name = _game_name(details)
        what = f"the game {name}" if name else "a game"
        return f"Mario just launched {what}. Get comfy with him. {_EVENT_INSTRUCTION}"
    if event == "game_stop":
        name = _game_name(details)
        what = f"{name}" if name else "his game"
        duration = _number(details, "duration_s")
        took = f" after {_spoken_duration(duration)}" if duration else ""
        return (
            f"Mario just closed {what}{took}. Something like 'finally done?'. {_EVENT_INSTRUCTION}"
        )
    if event == "afk_end":
        duration = _number(details, "duration_s") or 0
        hobbies = _hobbies_phrase(details)
        pastime = (
            f" While he was gone you spent the time {hobbies}; you can mention it in passing "
            "with a small opinion (like 'finished two chapters, it was mid'), only if it fits."
            if hobbies
            else ""
        )
        return (
            f"Mario just came back to his desk after being away for {_spoken_duration(duration)}. "
            f"Greet him.{pastime} {_EVENT_INSTRUCTION}"
        )
    if event == "gpu_hot":
        temp = _number(details, "temp_c")
        heat = f" - the GPU is at {round(temp)} degrees" if temp else ""
        return f"Mario's PC is running hot{heat}. You grab your water bottle to cool off. {_EVENT_INSTRUCTION}"
    if event == "call_join_guess":
        return f"Someone seems to have just joined Mario's voice call. A small, quiet hello. {_EVENT_INSTRUCTION}"
    if event == "poe_item":
        return _poe_item_prompt(details)
    return None


def _poe_item_prompt(details: dict) -> str | None:
    """Mario price-checked an item (details filled by react_to_event from the
    item text: name, base, rarity, prices). Only with a real price - she
    never invents one; rares stay Exiled Exchange's job for now (WP-57)."""
    name = _text(details, "name")
    if not name:
        return None
    rarity = _text(details, "rarity").lower()
    base = _text(details, "base")
    what = f"{name}" + (
        f" ({rarity} {base})" if base and base != name else f" ({rarity})" if rarity else ""
    )
    prices = [str(p) for p in details.get("prices") or [] if str(p).strip()][:2]
    if prices:
        return (
            f"Mario just price-checked {what} in Path of Exile 2. Live market: {' | '.join(prices)}. "
            "Say ONE short spoken line (max ~20 words), in character, that names the item and its "
            "price the way a person says it out loud (round it: 'like forty divines', 'a couple of "
            "exalts'). Big value: loot-goblin hype. Worthless: tease him. No lists, no markdown."
        )
    return None  # no price: she stays quiet (react_to_event skips "unpriced" first)


async def _lyric_for(details: dict, config: FeltConfig) -> str | None:
    """Best-effort quotable lyric for a new track: the current line if one
    is active, else the chorus line. Only when Spotify is actually playing
    that same title (so the id/duration LRCLIB needs are real)."""
    if not config.spotify_refresh_token:
        return None
    from felt_core.lyrics_client import current_line, find_chorus, get_lyrics
    from felt_core.spotify_client import get_currently_playing

    title = _text(details, "title").lower()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
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
    """The forwarded item text -> the whitelisted fields build_prompt reads,
    priced (uniques and currency) with poe_price_check."""
    from felt_core.brain import poe_reactions

    # Raw, not _text(): that collapses whitespace, and the item text's lines matter.
    item = poe_reactions.parse_item_text(str(details.get("text") or "")[:6000])
    if item is None:
        logger.info("event reaction poe_item: not a PoE item text")
        return {}
    prices = await poe_reactions.price_items([item], config)
    logger.info("poe item: %s (%s) -> %s", item["name"], item["rarity"], prices or "no price")
    return {**item, "prices": prices}


async def _generate_line(event: str, prompt: str, config: FeltConfig, tone: str | None) -> str:
    # WP-40: brain.replies is felt-core's small public surface for reply
    # generation - this used to reach straight into brain.py's
    # underscore-prefixed (private-by-convention) internals instead.
    from felt_core.brain.replies import local_reply, music_taste_context

    # Music taste keeps her opinions of a track consistent ("oh, this one
    # again?").
    taste = music_taste_context(config) if event == "music_new_track" else []

    async with metrics.timed("event_reaction", route="local", model=config.local_llm_model):
        # No facts (an event remark doesn't need them); local_reply adds
        # the personality prompt and the length cap.
        line = await local_reply(
            taste + [{"role": "user", "content": prompt}], config, include_facts=False, tone=tone
        )
    return line.strip(" \n\"'")


async def react_to_event(
    event: str,
    details: dict | None,
    config: FeltConfig,
    session_id: str = "dashboard",
    tone: str | None = None,
) -> EventReaction:
    details = details if isinstance(details, dict) else {}
    table = policies(config)
    key = dedupe_key(event, details)

    since_screen = seconds_since_last_spoken_reaction(config.db_path, session_id)
    since_any_event = seconds_since_last_event_reaction(config.db_path)
    since_any = min((s for s in (since_screen, since_any_event) if s is not None), default=None)
    reason = decide(
        event,
        details,
        table,
        min_gap_s=config.event_reaction_min_gap_s,
        since_any_remark=since_any,
        since_event=seconds_since_last_event_reaction(config.db_path, event)
        if event in table
        else None,
        since_same=seconds_since_last_event_reaction(config.db_path, event, key) if key else None,
    )
    if reason is not None:
        logger.info("event reaction %s: no line (%s)", event, reason)
        return EventReaction(skipped=reason)

    lyric = _text(details, "lyric") or None
    if event == "music_new_track" and lyric is None:
        lyric = await _lyric_for(details, config)
    previous = last_event_reaction_line(config.db_path, event) if event == "unhushed" else None
    if event == "poe_item":
        details = await _poe_item_details(details, config)
        if not details.get("prices"):
            logger.info("event reaction poe_item: no line (unpriced)")
            return EventReaction(skipped="unpriced")
    prompt = build_prompt(event, details, lyric, previous)
    if prompt is None:
        logger.info(
            "event reaction %s: details too thin to say anything (%s)", event, sorted(details)
        )
        return EventReaction(skipped="missing_details")

    try:
        line = await _generate_line(event, prompt, config, tone)
        if event == "unhushed":
            line = cap_words(line, UNHUSHED_MAX_WORDS)
            if same_line(line, previous):
                logger.info("event reaction %s: repeated the last line, asking once more", event)
                line = cap_words(
                    await _generate_line(event, prompt, config, tone), UNHUSHED_MAX_WORDS
                )
    except local_llm.LocalLLMUnavailable:
        logger.warning("event reaction %s: felt-llm unavailable", event)
        raise
    except Exception:
        logger.exception("event reaction %s: line generation failed", event)
        return EventReaction(skipped="generation_failed")
    if not line or line.upper() == "NONE" or (event == "unhushed" and same_line(line, previous)):
        return EventReaction(skipped="generation_failed")

    # Cooldown state + chat history (her line shows in the chat window,
    # like a screen remark does) - but never a screen observation.
    save_event_reaction(config.db_path, session_id, event, key, line)
    save_message(config.db_path, session_id, "assistant", line)
    schedule_pick(config, session_id)  # WP-42.2: which asides wait in Talk
    logger.info("event reaction %s: %r", event, line)
    return EventReaction(line=line, emotion=table[event].emotion)
