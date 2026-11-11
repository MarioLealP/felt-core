"""DJ mode on/off, and what happens on each Spotify poll while it's on.

DJ mode on/off, and what happens on each Spotify poll while it's on (the
listening-history loop's poll - no second one). She keeps AHEAD picks queued
in Spotify, says a line as each of her picks comes on, records how each went,
and steps back when Mario takes over (plays something that isn't hers) or
leaves it paused.
"""

import asyncio
import logging
import time

import httpx2
from pydantic import BaseModel, Field

from felt_core.clients import spotify_client as sp
from felt_core.config import FeltConfig
from felt_core.core import features, llm_gate
from felt_core.db.kinds import APP_SESSION
from felt_core.dj import learning, picker, store

logger = logging.getLogger(__name__)

AHEAD = 2
PAUSE_ENDS_S = 10 * 60
_LINE_TTL_S = 90.0
NOT_AWAKE = "Spotify's asleep on your desktop - hit play on anything once, then ask me again."


class _Session(BaseModel):
    on: bool = False
    current: dict | None = None  # her pick playing now
    ahead: list[dict] = Field(default_factory=list)  # queued in Spotify, not started yet
    paused_since: float | None = None
    refilling: bool = False


SESSION = _Session()
_tasks: set[asyncio.Task] = set()


def is_on() -> bool:
    """Whether DJ mode is on."""
    return SESSION.on


def same_track(pick: dict, snapshot: dict | None) -> bool:
    """Pure: is `snapshot` (a currently-playing poll) her `pick`?

    By uri, or by title + artist (Spotify can relink a track to another
    uri).
    """
    track = (snapshot or {}).get("track") or {}
    if track.get("uri") == pick["uri"]:
        return True
    artists = sp.norm_name(" ".join(a.get("name", "") for a in track.get("artists", [])))
    return bool(sp.norm_name(track.get("name", ""))) and (
        sp.norm_name(track.get("name", "")) == sp.norm_name(pick["title"]) and sp.norm_name(pick["artist"]) in artists
    )


def _uri(snapshot: dict | None) -> str | None:
    return ((snapshot or {}).get("track") or {}).get("uri")


async def start(config: FeltConfig) -> str:
    """Turn DJ mode on: start her first pick and queue the next ones; returns what to say."""
    async with httpx2.AsyncClient(timeout=20) as client:
        picks = await picker.pick(client, config, AHEAD + 1, avoid=set())
        if not picks:
            return "I couldn't line anything up right now."
        first, rest = picks[0], picks[1:]
        try:
            await sp.start_track(client, config, first["uri"], first.get("title"))
            for p in rest:
                await sp.queue_track(client, config, p["uri"])
        except sp.NoActivePlayerError:
            return NOT_AWAKE
        except httpx2.HTTPError:
            logger.warning("dj: Spotify refused the start", exc_info=True)
            return "Spotify didn't take that right now."
    for p in picks:
        store.add_pick(config.db_path, p["uri"], p["title"], p["artist"], p["discovery"])
    SESSION.on, SESSION.current, SESSION.ahead = True, first, rest
    SESSION.paused_since = None
    logger.info("dj: on, %d picks (%d new)", len(picks), sum(p["discovery"] for p in picks))
    new = " - new for you" if first["discovery"] else ""
    return f"DJ mode on. First up: {first['title']} by {first['artist']}{new}."


def stop(reason: str) -> None:
    """Turn DJ mode off, logging why."""
    if SESSION.on:
        logger.info("dj: off (%s)", reason)
    SESSION.on, SESSION.current, SESSION.ahead, SESSION.paused_since = False, None, [], None


def _paused(now: float) -> None:
    """Nothing playing: the set ends once it has been paused for PAUSE_ENDS_S."""
    SESSION.paused_since = SESSION.paused_since or now
    if now - SESSION.paused_since > PAUSE_ENDS_S:
        stop("paused for a while")


def _record_previous(config: FeltConfig, previous: dict | None) -> None:
    """How her pick that just ended went, unless she skipped it herself."""
    cur = SESSION.current
    if cur is not None and previous is not None and same_track(cur, previous) and not sp.felt_skipped(_uri(previous)):
        store.set_outcome(
            config.db_path,
            cur["uri"],
            learning.outcome(previous.get("progress_ms", 0), previous.get("duration_ms", 0)),
        )


def _not_hers(config: FeltConfig, current: dict) -> None:
    """A song that isn't one of her queued picks came on."""
    if sp.felt_started(_uri(current)):
        # a song he asked her for mid-set: an interlude, the set goes on
        SESSION.current = None
        return
    if _uri(current) in store.recent_pick_uris(config.db_path, 1):
        # Spotify's queue can't be cleared: a pick left over from an
        # earlier set came up - skip past it rather than stepping back
        _spawn(_skip_leftover(config))
        return
    stop("Mario played something else")


def _advance(config: FeltConfig, at: int) -> None:
    """Her pick at `at` in the queue came on: the ones before it were skipped; say a line, top up."""
    for passed in SESSION.ahead[:at]:  # skipped through within one poll
        if not sp.felt_skipped(passed["uri"]):
            store.set_outcome(config.db_path, passed["uri"], "skipped")
    SESSION.current, SESSION.ahead = SESSION.ahead[at], SESSION.ahead[at + 1 :]
    _spawn(_say(config, SESSION.current))
    if len(SESSION.ahead) < AHEAD:
        _spawn(_refill(config))


def on_poll(config: FeltConfig, previous: dict | None, current: dict | None) -> None:
    """Each Spotify poll (brain/background.py music_taste_loop)."""
    if not SESSION.on:
        return
    if current is None or not current.get("is_playing"):
        _paused(time.monotonic())
        return
    SESSION.paused_since = None
    if _uri(previous) == _uri(current):
        return
    _record_previous(config, previous)
    if SESSION.current is not None and same_track(SESSION.current, current):
        return
    at = next((i for i, p in enumerate(SESSION.ahead) if same_track(p, current)), None)
    if at is None:
        _not_hers(config, current)
    else:
        _advance(config, at)


async def feedback(config: FeltConfig, liked: bool) -> str:
    """Mario's "more like this" / "not this", remembered for her next picks; "not this" also skips it."""
    async with httpx2.AsyncClient(timeout=20) as client:
        now = await sp.get_currently_playing(client, config)
        if now is None:
            return "Nothing's playing right now."
        track = now["track"]
        title = track.get("name", "")
        artist = ", ".join(a["name"] for a in track.get("artists", []))
        store.add_feedback(config.db_path, title, artist, liked)
        if liked:
            return f"Noted - more like {title}."
        if SESSION.current is not None and same_track(SESSION.current, now):
            store.set_outcome(config.db_path, SESSION.current["uri"], "skipped")
        await sp.next_track(client, config)
    return f"Got it, no more {title}. Skipped."


async def _skip_leftover(config: FeltConfig) -> None:
    try:
        async with httpx2.AsyncClient(timeout=20) as client:
            await sp.next_track(client, config)
        logger.info("dj: skipped a leftover from an earlier set")
    except httpx2.HTTPError:
        logger.warning("dj: couldn't skip a leftover", exc_info=True)


async def _refill(config: FeltConfig) -> None:
    if SESSION.refilling or not SESSION.on:
        return
    SESSION.refilling = True
    try:
        queued = {p["uri"] for p in SESSION.ahead}
        if SESSION.current:
            queued.add(SESSION.current["uri"])
        async with httpx2.AsyncClient(timeout=20) as client:
            picks = await picker.pick(client, config, AHEAD - len(SESSION.ahead), avoid=queued)
            for p in picks:
                if not SESSION.on:
                    return
                await sp.queue_track(client, config, p["uri"])
                store.add_pick(config.db_path, p["uri"], p["title"], p["artist"], p["discovery"])
                SESSION.ahead.append(p)
        if picks:
            logger.info("dj: queued %d more", len(picks))
    except Exception:
        logger.warning("dj: refill failed", exc_info=True)
    finally:
        SESSION.refilling = False


async def _say(config: FeltConfig, pick: dict) -> None:
    from felt_core.core.events import react_to_event
    from felt_core.core.proactive import enqueue_proactive

    if not features.enabled(config.db_path, "ambient.dj_lines"):
        return
    details = {
        "title": pick["title"],
        "artist": pick["artist"],
        "new": pick["discovery"],
        "because": pick.get("because", ""),
    }
    try:
        with llm_gate.ambient("dj line", lifetime_s=_LINE_TTL_S):
            result = await react_to_event("dj_track", details, config, session_id=APP_SESSION)
    except Exception:
        logger.warning("dj: no line for %s", pick["title"], exc_info=True)
        return
    if result.line:
        enqueue_proactive(config, "music", result.line, emotion=result.emotion, ttl_s=_LINE_TTL_S, record=False)


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
