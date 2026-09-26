"""The two background loops started from api.py's lifespan: the weather
precompute (WP-06 - keeps a ready-to-serve "what's the weather right now"
answer warm) and the music-taste poll (WP-16 Part 2 - builds the
skip/full-play listening history that feeds back into the chat prompt).
Split out of brain.py (WP-40) since neither has anything to do with
per-request chat orchestration or reply phrasing - they're pure
fire-and-forget asyncio tasks, module-level state and all."""

import asyncio
import logging
import time

import httpx

from felt_core import tts_cache
from felt_core.brain.personality import PERSONALITY_PROMPT
from felt_core.brain.replies import _SKILL_PHRASING_MAX_TOKENS, _tool_result_instruction
from felt_core.cloud import cloud_generate
from felt_core.config import FeltConfig
from felt_core.db import record_music_play
from felt_core.music_taste import is_skip
from felt_core.plugin import ExecutionContext
from felt_core.plugin_loader import load_plugins
from felt_core.speech import strip_for_tts
from felt_core.spotify_client import get_currently_playing
from felt_core.tools import run_skill

logger = logging.getLogger(__name__)

# WP-06: "what's the weather right now?" measured 33s end to end (17.4s of
# it just local phrasing) - a background loop keeps a ready-to-serve
# answer warm so the common case skips the phrasing model AND TTS
# entirely. Module-level, not per-request state; refreshed by
# refresh_weather_precompute() (started from api.lifespan) every
# _WEATHER_PRECOMPUTE_INTERVAL_S, and read by chat()'s fast path
# (orchestrator.py, via _weather_precompute_fresh below).
_WEATHER_PRECOMPUTE_INTERVAL_S = 15 * 60
_WEATHER_PRECOMPUTE_MAX_AGE_S = 20 * 60
_WEATHER_NOW_PHRASES = {"now", "right now", "currently", "current", ""}
_weather_precompute: dict | None = None


def _weather_precompute_fresh() -> dict | None:
    if _weather_precompute is None:
        return None
    if time.monotonic() - _weather_precompute["computed_at"] > _WEATHER_PRECOMPUTE_MAX_AGE_S:
        return None
    return _weather_precompute


async def refresh_weather_precompute(config: FeltConfig) -> None:
    """One full precompute pass: run get_weather("now") for real, phrase
    it (cloud, same as a live request would), pre-render its TTS, and warm
    the TTS cache with that exact text - so chat()'s fast path and
    api._synthesize_reply's cache lookup are naturally consistent (both
    key off the identical `line` string, no separate coordination)."""
    global _weather_precompute

    registry = load_plugins(config.plugins_dir)
    if "get_weather" not in registry:
        return

    ctx = ExecutionContext(config=config, original_message="what's the weather right now?")
    try:
        result = await run_skill("get_weather", {"day": "now"}, registry, ctx)
    except ValueError:
        logger.warning("weather precompute: get_weather isn't registered, skipping")
        return
    tool_output = result.voice_line or result.text or ""

    line = tool_output
    if config.gemini_api_key:
        try:
            cloud_line = await cloud_generate(
                "skill_phrasing",
                PERSONALITY_PROMPT,
                [
                    {"role": "user", "content": "what's the weather right now?"},
                    {"role": "user", "content": _tool_result_instruction(tool_output)},
                ],
                config,
                max_tokens=_SKILL_PHRASING_MAX_TOKENS,
            )
            # Same "empty isn't success" rule as _phrase_skill_result -
            # cloud_generate can return "" deliberately (all-thinking, no
            # real answer) without raising.
            if cloud_line:
                line = cloud_line
            else:
                logger.warning(
                    "weather precompute: cloud phrasing returned nothing usable, using the raw tool output"
                )
        except Exception:
            logger.exception("weather precompute: cloud phrasing failed, using the raw tool output")

    tts_text = strip_for_tts(line)
    # Same line as last time (the weather didn't change) - already cached,
    # no need to re-render it.
    if config.voice_url and tts_cache.get(tts_text) is None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                tts_resp = await client.post(f"{config.voice_url}/v1/tts", json={"text": tts_text})
                tts_resp.raise_for_status()
            tts_cache.put(tts_text, tts_resp.content)
        except httpx.HTTPError as e:
            # The 2026-09-24 audit's ~76 failures/24h were all
            # httpcore.ConnectError: felt-voice on the Nitro5 was down
            # (09-23 22:30 -> 09-24 17:45), not a felt-core bug. One line,
            # not a full traceback every 15 minutes; a live request just
            # renders TTS itself on a cache miss.
            logger.warning(
                "weather precompute: TTS pre-render skipped, felt-voice unavailable (%r)", e
            )
        except Exception:
            logger.exception("weather precompute: TTS pre-render failed")

    _weather_precompute = {
        "line": line,
        "animation_tag": result.animation_tag,
        "computed_at": time.monotonic(),
    }
    logger.info("weather precompute refreshed: %r", line)


def current_weather() -> dict:
    """WP-21: the precompute's current condition for the desktop (props:
    umbrella when it rains, sunglasses when it's sunny) - "rain" / "sun" /
    "snow" / "storm" from the get_weather condition tag, None when the
    code has no mapping or nothing fresh is cached. No text, just the
    condition."""
    fresh = _weather_precompute_fresh()
    if fresh is None:
        return {"condition": None, "animation_tag": None, "age_s": None}
    tag = fresh.get("animation_tag")
    condition = (
        tag.rsplit("/", 1)[-1]
        if isinstance(tag, str) and tag.startswith("reaction/weather/")
        else None
    )
    return {
        "condition": condition,
        "animation_tag": tag,
        "age_s": round(time.monotonic() - fresh["computed_at"]),
    }


async def weather_precompute_loop(config: FeltConfig) -> None:
    while True:
        try:
            await refresh_weather_precompute(config)
        except Exception:
            logger.exception("weather precompute loop iteration failed")
        await asyncio.sleep(_WEATHER_PRECOMPUTE_INTERVAL_S)


_MUSIC_TASTE_POLL_INTERVAL_S = 15.0


def _track_key(track: dict | None) -> str | None:
    """A stable identity for a Spotify item. Local files (and some other
    non-catalogue items) come back with `id: null` but still carry a
    `uri` ("spotify:local:..."), so that's the fallback key. None means
    the item can't be keyed at all and is simply not tracked - the
    music_taste table's track_id is NOT NULL, and a null id there wedged
    music_taste_loop on one item every 15 s (audit 2026-09-24)."""
    if not track:
        return None
    return track.get("id") or track.get("uri") or None


def _finished_play_from_transition(previous: dict | None, current: dict | None) -> dict | None:
    """Pure: `previous`/`current` are get_currently_playing snapshots (or
    None) from two consecutive polls. A listen "finishes" - and gets
    returned as a record ready for db.record_music_play(**record) - when
    the track id changed or playback stopped since `previous`; returns
    None when the same track is still playing, or there was nothing
    playing before to finish. `played_ms` is `previous`'s last-seen
    progress, not a precise stop time - it can undercount actual listen
    time by up to one poll interval, an accepted approximation."""
    if previous is None:
        return None
    prev_track = previous["track"]
    prev_key = _track_key(prev_track)
    if prev_key is None:
        # Nothing to key the listen on (no id, no uri) - not recordable.
        return None
    current_track = current["track"] if current else None
    if current_track is not None and _track_key(current_track) == prev_key:
        return None
    return {
        "track_id": prev_key,
        "track_name": prev_track.get("name", ""),
        "artists": ", ".join(a["name"] for a in prev_track.get("artists", [])),
        "played_ms": previous["progress_ms"],
        "duration_ms": previous["duration_ms"],
        "skipped": is_skip(previous["progress_ms"], previous["duration_ms"]),
    }


async def music_taste_loop(config: FeltConfig) -> None:
    """WP-16 Part 2: polls Spotify's currently-playing endpoint to build a
    skip/full-play history per track (see music_taste.summarize_taste for
    how it's surfaced back into the prompt). Runs independently of
    felt-tray's own /v1/music/now polling (that one drives unprompted
    "oh, this one again" remarks, not taste tracking) - both happen to
    poll at a similar cadence for different reasons, and neither depends
    on the other."""
    previous: dict | None = None
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            try:
                if config.spotify_refresh_token:
                    current = await get_currently_playing(client, config)
                    finished = _finished_play_from_transition(previous, current)
                    # Advance BEFORE recording: a record that fails must
                    # never leave `previous` pointing at the same item, or
                    # every later poll retries (and fails on) it forever.
                    previous = current
                    if finished:
                        try:
                            record_music_play(config.db_path, **finished)
                        except Exception:
                            logger.exception(
                                "music taste: couldn't record %r, skipping it",
                                finished.get("track_name"),
                            )
            except Exception:
                logger.exception("music taste loop iteration failed")
            await asyncio.sleep(_MUSIC_TASTE_POLL_INTERVAL_S)
