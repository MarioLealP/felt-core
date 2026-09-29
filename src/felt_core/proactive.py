"""WP-25: the proactive channel, felt-core -> desktop. A small SQLite queue
(db.py's `proactive` table) that other WPs enqueue into (WP-26's diary
morning greeting, WP-31's calendar heads-up), drained by felt-tray's
long-poll against `GET /v1/proactive/next`.

Kept out of `api.py` (which just wires the HTTP surface) and `db.py`
(pure SQL) - this is the poll-with-timeout logic plus `enqueue_proactive`,
the one function other features call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from felt_core import db, metrics, tts_cache
from felt_core.speech import strip_for_tts, tts_request_body

logger = logging.getLogger(__name__)

# How often wait_for_next re-checks the DB while long-polling. Plain
# asyncio.sleep, not an asyncio.Event wakeup: felt-core is a single
# process, so a 0.5s worst-case delivery delay is not worth the extra
# lifecycle complexity of an Event that would need to be tied to whichever
# event loop is currently running (real risk in the test suite, which
# spins up a fresh loop per async test - a module-level Event bound to an
# earlier loop raises "bound to a different event loop" on the next one).
_POLL_INTERVAL_S = 0.5
_MIN_TIMEOUT_S = 1.0
_MAX_TIMEOUT_S = 30.0
_DEFAULT_TTL_S = 600.0


# Audio synthesis in flight, keyed on the TTS text. The accept test
# measured ~3 s enqueue->playback, 2.1 s of it an on-demand felt-voice call
# made only when felt-tray fetched /audio. Synthesis now starts at enqueue
# time. A fetch that lands mid-synthesis awaits the same task rather than
# starting a second felt-voice call. A task from another event loop (tests
# run a fresh loop each) is ignored, never awaited.
_inflight: dict[tuple[str, str | None], asyncio.Task[bytes]] = {}


async def _synthesize(config: Any, tts_text: str, emotion: str | None = None) -> bytes:
    cached = tts_cache.get(tts_text, emotion)
    if cached is not None:
        return cached
    async with (
        httpx.AsyncClient(timeout=60) as client,
        metrics.timed("tts", chars=len(tts_text)),
    ):
        resp = await client.post(
            f"{config.voice_url}/v1/tts", json=tts_request_body(tts_text, emotion)
        )
        resp.raise_for_status()
    tts_cache.put(tts_text, resp.content, emotion)
    return resp.content


async def proactive_audio(config: Any, text: str, emotion: str | None = None) -> bytes:
    """The item's WAV via the shared WP-06 TTS cache, joining an in-flight
    pre-synthesis if there is one."""
    tts_text = strip_for_tts(text)
    cached = tts_cache.get(tts_text, emotion)
    if cached is not None:
        return cached
    task = _inflight.get((tts_text, emotion))
    if task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop():
        return await task
    return await _synthesize(config, tts_text, emotion)


def _prewarm_audio(config: Any, text: str, emotion: str | None = None) -> None:
    """Best-effort: start synthesis now if we're inside the server's event
    loop and felt-voice is configured. Enqueueing from sync code with no
    running loop (scripts, tests) just skips it, and /audio synthesises on
    demand as before."""
    if not getattr(config, "voice_url", None):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    tts_text = strip_for_tts(text)
    key = (tts_text, emotion)
    if tts_cache.get(tts_text, emotion) is not None or key in _inflight:
        return

    async def _run() -> bytes:
        try:
            return await _synthesize(config, tts_text, emotion)
        finally:
            _inflight.pop(key, None)

    task = loop.create_task(_run())
    task.add_done_callback(
        lambda t: (
            t.exception() and logger.warning("proactive pre-synthesis failed: %r", t.exception())
        )
    )
    _inflight[key] = task


def clamp_timeout(timeout_s: float) -> float:
    """The long-poll endpoint's own `?timeout=` clamped to a sane band -
    long enough to actually long-poll, short enough a client's HTTP
    timeout/connection never has to outlive it by much."""
    return max(_MIN_TIMEOUT_S, min(_MAX_TIMEOUT_S, timeout_s))


def enqueue_proactive(
    config: Any,
    kind: str,
    text: str,
    *,
    animation_tag: str | None = None,
    emotion: str | None = None,
    ttl_s: float = _DEFAULT_TTL_S,
) -> int:
    """Inserts one proactive item, picked up by the next `wait_for_next`
    poll tick (at most `_POLL_INTERVAL_S` later). `ttl_s` is how long the
    item stays worth delivering (WP-25: "a heads-up for an event that has
    already started is useless") - past that, `claim_next_proactive`/the
    accept test's "an expired one never is [spoken]" guarantee kicks in."""
    payload: dict[str, str] = {}
    if animation_tag is not None:
        payload["animation_tag"] = animation_tag
    if emotion is not None:
        payload["emotion"] = emotion
    expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_s)).strftime("%Y-%m-%d %H:%M:%S")
    item_id = db.insert_proactive(config.db_path, kind, text, json.dumps(payload), expires_at)
    if ttl_s > 0:
        _prewarm_audio(config, text, emotion)
    return item_id


def _item_to_response(row: dict) -> dict:
    payload = json.loads(row["payload_json"] or "{}")
    return {
        "id": row["id"],
        "kind": row["kind"],
        "text": row["text"],
        "animation_tag": payload.get("animation_tag"),
        "emotion": payload.get("emotion"),
    }


async def wait_for_next(config: Any, timeout_s: float) -> dict | None:
    """Long-poll: returns the oldest undelivered, unexpired item (already
    marked delivered by `claim_next_proactive`) as soon as one exists, or
    None once `timeout_s` (already clamped by the caller) elapses without
    one."""
    deadline = time.monotonic() + timeout_s
    while True:
        row = db.claim_next_proactive(config.db_path)
        if row is not None:
            return _item_to_response(row)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(remaining, _POLL_INTERVAL_S))
