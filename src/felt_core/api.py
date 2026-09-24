"""The frontend API — what a dashboard, voice pipeline, or Android client
actually talks to. None of them touch brain internals directly.

v1, deliberately simple: one request in, one reply out, no session/
conversation-history handling yet (that's SQLite persistence, a separate
near-term addition — see assistant-core.md's Build phases; this phase is
just about having *an* API surface at all, versioned so it can evolve).
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from felt_core import metrics, tts_cache
from felt_core.alarms import reschedule_pending_alarms
from felt_core.brain import chat, music_taste_loop, process_screen_context, react_to_screen, weather_precompute_loop
from felt_core.cloud import CloudUnavailable, cloud_status
from felt_core.config import load_config
from felt_core.db import (
    add_shopping_item,
    clear_checked_shopping_items,
    delete_old_latency_rows,
    delete_shopping_item,
    get_history,
    get_latency_rows,
    get_setting,
    get_shopping_items,
    set_setting,
    set_shopping_item_checked,
)
from felt_core.email_watcher import watch as email_watch
from felt_core.ntfy_listener import listen as ntfy_listen
from felt_core.poe_trade_cache import watch as poe_trade_cache_watch
from felt_core.lyrics_client import current_line, find_chorus, get_lyrics, with_end_times
from felt_core.speech import strip_for_tts
from felt_core.spotify_client import format_track_name, get_currently_playing

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    ntfy_task = asyncio.create_task(ntfy_listen(config))
    email_task = asyncio.create_task(email_watch(config))
    poe_cache_task = asyncio.create_task(poe_trade_cache_watch(config))
    # WP-06: keeps a ready-to-serve "what's the weather right now" answer
    # (text + pre-rendered TTS) warm at all times, refreshed every 15 min -
    # see brain.py's weather_precompute_loop/refresh_weather_precompute.
    weather_task = asyncio.create_task(weather_precompute_loop(config))
    # WP-16 Part 2: builds the skip/full-play listening history that
    # music_taste.summarize_taste folds back into the chat prompt.
    music_taste_task = asyncio.create_task(music_taste_loop(config))
    # A pending alarm set before a restart isn't just lost — see
    # alarms.py for why late-firing beats silently dropping it.
    reschedule_pending_alarms(config)
    # WP-04: latency rows accumulate one row per stage per request forever
    # otherwise - a startup sweep is simpler than a second scheduled task
    # for what's meant to be a bounded rolling window, not permanent history.
    try:
        delete_old_latency_rows(config.db_path, older_than_days=30)
    except Exception:
        logger.exception("failed to prune old latency rows")
    yield
    ntfy_task.cancel()
    email_task.cancel()
    poe_cache_task.cancel()
    weather_task.cancel()
    music_taste_task.cancel()


app = FastAPI(title="felt-core", version="0.1.0", lifespan=lifespan)

# LAN-only right now, no auth on this API yet (same posture as
# Ollama/ComfyUI elsewhere in this project) — permissive CORS matches
# that reality rather than pretending there's a security boundary here.
# Tighten this (a real origin allowlist, or auth) before this is ever
# reachable outside the LAN.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-Transcribed-Text",
        "X-Reply-Text",
        "X-UI-Action",
        "X-Animation-Tag",
        "Server-Timing",
        "X-Emotion",
    ],
)


@app.middleware("http")
async def latency_middleware(request: Request, call_next):
    """Times every /v1/* request end-to-end and stamps a standard
    Server-Timing header on the response, built from whatever brain.py/
    api.py stages recorded via metrics.timed() during the request (WP-04).
    Non-/v1/ paths (static assets, /openapi.json) aren't worth the
    request_id/SQLite overhead."""
    if not request.url.path.startswith("/v1/"):
        return await call_next(request)

    config = load_config()
    session_id = request.query_params.get("session_id")
    metrics.start_request(str(uuid.uuid4()), request.url.path, config.db_path, session_id)

    start = time.monotonic()
    response = await call_next(request)
    metrics.record("total", (time.monotonic() - start) * 1000.0)

    header = metrics.server_timing_header()
    if header:
        response.headers["Server-Timing"] = header
    return response


@app.get("/v1/metrics/latency")
async def latency_metrics_endpoint(since_hours: int = 24, endpoint: str | None = None) -> dict:
    config = load_config()
    rows = get_latency_rows(config.db_path, since_hours=since_hours, endpoint=endpoint)
    return metrics.compute_stats(rows)


@app.get("/v1/metrics/cloud")
async def cloud_metrics_endpoint() -> dict:
    config = load_config()
    return cloud_status(config)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    # WP-15: felt-animation's GET /tone line (mood x time-of-day + streak
    # awareness), fetched and attached by felt-tray/felt-dashboard. Optional
    # so an older client (or one with no felt-animation running) is unaffected.
    tone: str | None = None


class ChatResponse(BaseModel):
    reply: str
    # Both purely additive - an older client that only reads `reply` is
    # unaffected. ui_action mirrors SkillResult.ui_action (e.g.
    # {"kind": "toast", "payload": {...}}); animation_tag is what
    # felt-tray should relay to felt-overlay (see felt_core.animation).
    # Both are None for a plain conversational reply with no triggered
    # skill, or a skill whose tag/ui_action just haven't been set yet.
    ui_action: dict[str, Any] | None = None
    animation_tag: str | None = None


@app.post("/v1/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest) -> ChatResponse:
    config = load_config()
    result = await chat(req.message, config, req.session_id, req.tone)
    return ChatResponse(
        reply=result.reply,
        ui_action=asdict(result.ui_action) if result.ui_action else None,
        animation_tag=result.animation_tag,
    )


@app.get("/v1/history")
async def history_endpoint(session_id: str = "default", limit: int = 100) -> list[dict]:
    """A client's chat window is otherwise blank on every restart even
    though the conversation is already durable in SQLite (`get_history`
    was previously only read internally, to build the brain's own
    context) — this just exposes that same data so a dashboard can
    restore what was actually said."""
    config = load_config()
    return get_history(config.db_path, session_id, limit)


async def _synthesize_reply(
    reply: str,
    ui_action: dict[str, Any] | None,
    animation_tag: str | None,
    config: Any,
    client: httpx.AsyncClient,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """Shared by voice-chat and text-chat: TTS the reply and build the same
    audio/wav + X-* header response shape, so a client doesn't need to know
    or care which input mode produced it."""
    tts_text = strip_for_tts(reply)
    logger.debug("TTS input after strip_for_tts: %r", tts_text)

    # WP-06: a cache hit here (warmed by brain.py's weather precompute
    # loop) means the common "what's the weather right now?" turn makes
    # zero calls to felt-voice at all.
    audio_bytes = tts_cache.get(tts_text)
    if audio_bytes is None:
        async with metrics.timed("tts", chars=len(tts_text)):
            tts_resp = await client.post(f"{config.voice_url}/v1/tts", json={"text": tts_text})
            tts_resp.raise_for_status()
        audio_bytes = tts_resp.content
        tts_cache.put(tts_text, audio_bytes)

    # Percent-encoded — headers must be ASCII-safe, a non-English reply
    # (accents, etc.) would otherwise break this.
    headers = {"X-Reply-Text": quote(reply), **(extra_headers or {})}
    if ui_action is not None:
        headers["X-UI-Action"] = quote(json.dumps(ui_action))
    if animation_tag is not None:
        headers["X-Animation-Tag"] = quote(animation_tag)

    return Response(content=audio_bytes, media_type="audio/wav", headers=headers)


@app.post("/v1/voice-chat")
async def voice_chat_endpoint(
    file: UploadFile = File(...), session_id: str = "default", tone: str | None = Form(None)
) -> Response:
    """One call: audio in, audio out. Orchestrates felt-voice (STT) ->
    the brain -> felt-voice (TTS), so a client doesn't have to."""
    config = load_config()
    if not config.voice_url:
        raise HTTPException(status_code=503, detail="Voice isn't configured (no FELT_VOICE_URL set).")

    async with httpx.AsyncClient(timeout=60) as client:
        async with metrics.timed("stt"):
            stt_resp = await client.post(
                f"{config.voice_url}/v1/stt", files={"file": (file.filename, await file.read(), file.content_type)}
            )
            stt_resp.raise_for_status()
            transcribed = stt_resp.json()["text"]

        ui_action: dict[str, Any] | None = None
        animation_tag: str | None = None
        if transcribed.strip():
            result = await chat(transcribed, config, session_id, tone)
            reply = result.reply
            ui_action = asdict(result.ui_action) if result.ui_action else None
            animation_tag = result.animation_tag
        else:
            # Real bug found live, 2026-09-12: an empty/silent recording
            # (e.g. a wheel-tilt that immediately auto-stopped on silence,
            # see felt-desktop-agent's audio.rs) was still handed to the
            # router as a real message, which — faced with nothing to go
            # on — guessed a no-required-params tool (get_weather) rather
            # than the router having any "actually, nothing was said"
            # option. Skipping the router entirely when there's no
            # transcript at all is the honest answer, not a guess.
            reply = "I didn't catch that."

        return await _synthesize_reply(
            reply, ui_action, animation_tag, config, client, {"X-Transcribed-Text": quote(transcribed)}
        )


@app.post("/v1/text-chat")
async def text_chat_endpoint(req: ChatRequest) -> Response:
    """Same shape as /v1/voice-chat's response (audio/wav + X-Reply-Text/
    X-Animation-Tag headers) but for already-typed text - so a typed
    message gets the same TTS + reaction-animation treatment a spoken one
    does, minus the STT step (Mario, 2026-09-21: "hook up the chat like it
    was voice input, need her to reply with TTS + animation")."""
    config = load_config()
    if not config.voice_url:
        raise HTTPException(status_code=503, detail="Voice isn't configured (no FELT_VOICE_URL set).")

    result = await chat(req.message, config, req.session_id, req.tone)
    async with httpx.AsyncClient(timeout=60) as client:
        return await _synthesize_reply(
            result.reply,
            asdict(result.ui_action) if result.ui_action else None,
            result.animation_tag,
            config,
            client,
        )


class ScreenContextRequest(BaseModel):
    description: str
    tone: str | None = None


@app.post("/v1/context/screen")
async def screen_context_endpoint(req: ScreenContextRequest, session_id: str = "default") -> Response:
    """Receives passive screen observations from felt-vision.
    Returns audio+animation headers just like chat, but handles pure-silent
    reactions by skipping TTS entirely."""
    config = load_config()
    result = await process_screen_context(req.description, config, session_id, req.tone)

    if not result.reply.strip():
        # Pure silent body-language reaction
        headers = {}
        if result.animation_tag is not None:
            headers["X-Animation-Tag"] = quote(result.animation_tag)
        return Response(content=b"", media_type="audio/wav", headers=headers)
        
    async with httpx.AsyncClient(timeout=60) as client:
        return await _synthesize_reply(
            result.reply,
            asdict(result.ui_action) if result.ui_action else None,
            result.animation_tag,
            config,
            client,
        )


@app.post("/v1/react/screen")
async def react_screen_endpoint(
    image: UploadFile = File(...),
    app_name: str = Form(..., alias="app"),
    title: str = Form(""),
    trigger: str = Form("focus"),
    session_id: str = "dashboard",
    tone: str | None = Form(None),
) -> Response:
    """WP-07: felt-tray's new per-window capture path - the JPEG has
    already cleared the blocklist client-side before this is ever called.
    A single multimodal cloud_generate call, no local-Ollama fallback
    (there's no local VLM on this server) - CloudUnavailable maps to a
    503, and felt-tray's watcher falls back to the old felt-vision +
    /v1/context/screen path itself when it sees that."""
    config = load_config()
    jpeg_bytes = await image.read()
    try:
        result = await react_to_screen(
            jpeg_bytes, app_name, title, config, session_id=session_id, trigger=trigger, tone=tone
        )
    except CloudUnavailable:
        return JSONResponse(status_code=503, content={"detail": "cloud_unavailable"})

    emotion_headers = {"X-Emotion": quote(result.emotion)} if result.emotion else {}

    if not result.reply.strip():
        headers = dict(emotion_headers)
        if result.animation_tag is not None:
            headers["X-Animation-Tag"] = quote(result.animation_tag)
        return Response(content=b"", media_type="audio/wav", headers=headers)

    async with httpx.AsyncClient(timeout=60) as client:
        return await _synthesize_reply(
            result.reply,
            asdict(result.ui_action) if result.ui_action else None,
            result.animation_tag,
            config,
            client,
            extra_headers=emotion_headers or None,
        )


class TemperatureUnitRequest(BaseModel):
    unit: Literal["celsius", "fahrenheit"]


@app.post("/v1/settings/temperature-unit")
async def set_temperature_unit(req: TemperatureUnitRequest) -> dict:
    config = load_config()
    set_setting(config.db_path, "temperature_unit", req.unit)
    return {"unit": req.unit}


@app.get("/v1/settings/temperature-unit")
async def get_temperature_unit() -> dict:
    config = load_config()
    return {"unit": get_setting(config.db_path, "temperature_unit", "celsius")}


@app.get("/v1/music/now")
async def music_now_endpoint() -> dict:
    """WP-16 Part 2: what's currently playing on Spotify - polled by
    felt-tray every ~15s to drive unprompted "oh, this one again" remarks
    on a track change, and answerable directly via the now_playing skill.
    `{"is_playing": false}` covers both "nothing loaded" and "Spotify not
    connected" - callers don't need to distinguish those."""
    config = load_config()
    if not config.spotify_refresh_token:
        return {"is_playing": False}

    async with httpx.AsyncClient(timeout=20) as client:
        now_playing = await get_currently_playing(client, config)

    if now_playing is None:
        return {"is_playing": False}

    track = now_playing["track"]
    return {
        "is_playing": now_playing["is_playing"],
        "track": track.get("name", ""),
        "artists": [a["name"] for a in track.get("artists", [])],
        "album": track.get("album", {}).get("name", ""),
        # A local file has id: null - its uri still identifies it.
        "track_id": track.get("id") or track.get("uri") or "",
        "display_name": format_track_name(track),
        "progress_ms": now_playing["progress_ms"],
        "duration_ms": now_playing["duration_ms"],
    }


@app.get("/v1/music/lyrics")
async def music_lyrics_endpoint() -> dict:
    """WP-16 Part 3: lyrics for whatever's currently playing, from LRCLIB
    (Spotify's own API has no lyrics endpoint). `current_line`/`chorus`
    are only populated when LRCLIB returned synced (LRC) lyrics - plenty
    of tracks only have plain text, which is still usable for a quoted
    remark but has no per-line timing to build them from."""
    config = load_config()
    if not config.spotify_refresh_token:
        return {"found": False}

    async with httpx.AsyncClient(timeout=20) as client:
        now_playing = await get_currently_playing(client, config)
        if now_playing is None:
            return {"found": False}

        track = now_playing["track"]
        lyrics = await get_lyrics(
            client,
            Path(config.lyrics_cache_dir),
            track.get("id", ""),
            track.get("name", ""),
            ", ".join(a["name"] for a in track.get("artists", [])),
            track.get("album", {}).get("name", ""),
            now_playing["duration_ms"] // 1000,
            user_agent=config.lyrics_user_agent,
        )

    if lyrics is None:
        return {"found": False}

    synced = lyrics["synced"]
    progress_ms = now_playing["progress_ms"]
    # `chorus` keeps its original start_ms/end_ms/text fields (now pointing
    # at the current-or-next chorus occurrence rather than always the
    # first) and adds `windows` (every occurrence, with per-line timings).
    # `lines` is the whole synced lyric with real start/end times, and
    # `progress_ms` the position they were computed against, so the
    # desktop can build a mouth-along timeline from real timings.
    return {
        "found": True,
        "has_synced": bool(synced),
        "current_line": current_line(synced, progress_ms) if synced else None,
        "chorus": find_chorus(synced, progress_ms) if synced else None,
        "lines": with_end_times(synced) if synced else [],
        "progress_ms": progress_ms,
        "plain_excerpt": (lyrics["plain"][:200] if lyrics["plain"] else None),
    }


class ShoppingItemRequest(BaseModel):
    text: str


@app.get("/v1/shopping-list")
async def get_shopping_list_endpoint() -> list[dict]:
    config = load_config()
    return get_shopping_items(config.db_path)


@app.post("/v1/shopping-list")
async def add_shopping_list_item_endpoint(req: ShoppingItemRequest) -> dict:
    config = load_config()
    item_id = add_shopping_item(config.db_path, req.text)
    return {"id": item_id}


@app.delete("/v1/shopping-list/checked")
async def clear_checked_shopping_list_endpoint() -> dict:
    # Registered before the /{item_id} routes below — FastAPI matches path
    # operations in declaration order, and "checked" would otherwise be
    # captured as a (non-integer, 422-rejected) item_id by DELETE
    # /v1/shopping-list/{item_id} instead of hitting this route.
    config = load_config()
    clear_checked_shopping_items(config.db_path)
    return {"ok": True}


@app.patch("/v1/shopping-list/{item_id}")
async def set_shopping_item_checked_endpoint(item_id: int, checked: bool) -> dict:
    config = load_config()
    set_shopping_item_checked(config.db_path, item_id, checked)
    return {"ok": True}


@app.delete("/v1/shopping-list/{item_id}")
async def delete_shopping_item_endpoint(item_id: int) -> dict:
    config = load_config()
    delete_shopping_item(config.db_path, item_id)
    return {"ok": True}


@app.get("/shopping-list")
async def shopping_list_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "shopping-list.html")


@app.websocket("/v1/chat/ws")
async def chat_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    config = load_config()
    try:
        while True:
            data = await websocket.receive_json()
            result = await chat(data["message"], config, data.get("session_id", "default"))
            await websocket.send_json(
                {
                    "reply": result.reply,
                    "ui_action": asdict(result.ui_action) if result.ui_action else None,
                    "animation_tag": result.animation_tag,
                }
            )
    except Exception:
        await websocket.close()


def run() -> None:
    # No logging config existed anywhere in this codebase - felt_core's own
    # logger.info() calls (e.g. brain.py's cloud-vs-local chat routing)
    # were silently dropped below the default WARNING threshold. Only
    # uvicorn's own access-log lines were ever visible.
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    uvicorn.run(app, host=config.host, port=config.port)
