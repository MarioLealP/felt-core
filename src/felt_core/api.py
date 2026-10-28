"""The frontend API — what a dashboard, voice pipeline, or Android client
actually talks to. None of them touch brain internals directly.

v1, deliberately simple: one request in, one reply out, no session/
conversation-history handling yet (that's SQLite persistence, a separate
near-term addition — see assistant-core.md's Build phases; this phase is
just about having *an* API surface at all, versioned so it can evolve).
"""

import asyncio
import contextlib
import hmac
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi import Path as FastAPIPath
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from felt_core import (
    diary,
    features,
    ha_auth,
    incidents,
    llm_gate,
    local_llm,
    machine_auth,
    metrics,
    reply_stream,
    shopping,
    tts_cache,
)
from felt_core.alarms import reschedule_pending_alarms
from felt_core.app_api import SESSION as APP_SESSION
from felt_core.app_api import router as app_router
from felt_core.app_api import schedule_mark
from felt_core.brain import (
    ChatResult,
    ReplyMeta,
    chat,
    chat_streamed,
    current_weather,
    llm_warmup_loop,
    music_taste_loop,
    poe_reactions,
    process_screen_context,
    react_to_screen,
    router,
    weather_precompute_loop,
)
from felt_core.calendar_api import router as calendar_router
from felt_core.calendar_headsup import calendar_headsup_loop
from felt_core.config import load_config
from felt_core.controls_api import router as controls_router
from felt_core.db import (
    delete_diary,
    delete_old_latency_rows,
    delete_self_memory,
    get_diary,
    get_diary_list,
    get_diary_weeks,
    get_history,
    get_latency_rows,
    get_proactive_item,
    get_self_memories,
    get_self_memory,
    get_setting,
    prune_expired_proactive,
    requeue_proactive,
    save_event,
    set_setting,
    update_diary_entry,
    update_self_memory,
)
from felt_core.emails import watch as email_watch
from felt_core.ender import watch as ender_watch
from felt_core.events import react_to_event
from felt_core.felt_tls import voice_client
from felt_core.ha_api import router as ha_router
from felt_core.incident_api import router as incident_router
from felt_core.lyrics_client import current_line, find_chorus, get_lyrics, with_end_times
from felt_core.notices_api import router as notices_router
from felt_core.ntfy_listener import listen as ntfy_listen
from felt_core.poe_trade_cache import watch as poe_trade_cache_watch
from felt_core.proactive import clamp_timeout, enqueue_proactive, proactive_audio, wait_for_next
from felt_core.speech import speech_chunks, strip_for_tts, tts_request_body
from felt_core.spotify_client import format_track_name, get_currently_playing
from felt_core.systems_api import router as systems_router

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    # WP-62 M1: say whether the vault path delivered (never the value itself)
    if config.vault_probe:
        logger.info("vault: probe delivered (%d chars)", len(config.vault_probe))
    else:
        logger.info("vault: no probe (settings not vault-delivered)")
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
    # cm-6b0: the semantic router, shadow mode - loads off the event loop
    router_task = asyncio.create_task(router.warm(config))
    # WP-36 D'': primes felt-llm's prompt cache at startup and whenever it
    # comes back after being unreachable (checked every 60 s).
    warmup_task = asyncio.create_task(llm_warmup_loop(config))
    # WP-31: T-30/T-10 min calendar heads-ups through the proactive channel.
    calendar_task = asyncio.create_task(calendar_headsup_loop(config))
    # WP-44: self-fixing alerts that didn't fix themselves become open, and
    # she writes her line about each incident that counts
    incidents_task = asyncio.create_task(incidents.watch(config))
    # the Ender (via HA): says when a print finishes, fails, is cancelled or pauses
    ender_task = asyncio.create_task(ender_watch(config))
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
    # WP-25: same startup-sweep convention - an expired proactive item is
    # already unreachable via claim_next_proactive, this just stops the
    # table growing forever.
    try:
        prune_expired_proactive(config.db_path)
    except Exception:
        logger.exception("failed to prune expired proactive items")
    yield
    ntfy_task.cancel()
    email_task.cancel()
    poe_cache_task.cancel()
    weather_task.cancel()
    music_taste_task.cancel()
    router_task.cancel()
    warmup_task.cancel()
    calendar_task.cancel()
    incidents_task.cancel()
    ender_task.cancel()


app = FastAPI(title="felt-core", version="0.1.0", lifespan=lifespan)
app.include_router(app_router, prefix="/v1/app")
app.include_router(app_router, prefix="/v1/ha/app")
app.include_router(incident_router)
app.include_router(systems_router)
app.include_router(notices_router)
app.include_router(calendar_router)
app.include_router(controls_router)
app.include_router(ha_router)

# No CORS (WP-62 phase 0.2, 2026-10-05): every browser client is same-origin
# - the Felt app is served from felt.clockmost.com/app next to this API, and
# the tray's window loads that hosted app; the tray, felt-animation and the
# responder are native clients. Without CORS headers a foreign site can't
# read any response, credentialed or not.


# Polling / status / metrics reads - not user-facing turns, never timed.
_UNTIMED_PATHS = (
    "/v1/metrics/",
    "/v1/music/now",
    "/v1/music/lyrics",
    "/v1/history",
    "/v1/health",
    "/v1/status",
    # WP-25: a long-poll that can sit open for up to 30s by design - not a
    # real turn, and would otherwise swamp the `total` stage's percentiles
    # the same way /v1/music/now's old un-excluded polling did (WP-04 fix).
    "/v1/proactive/next",
    # WP-54: a long-poll (up to 60s) for a streamed reply's next chunk.
    "/v1/reply/",
)


def _is_untimed(path: str) -> bool:
    return any(path.startswith(p) for p in _UNTIMED_PATHS)


# WP-55: what stays reachable without the token - the container healthcheck
# (/openapi.json, schema only, no data) and the docs page shell. OPTIONS
# carries no credentials and gets no CORS answer, so it passes too.
_AUTH_EXEMPT = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})
# The /shopping-list phone page and exactly the calls it makes. A browser
# can't hold the API token, so Caddy puts SSO in front of these paths
# instead (Mario 2026-10-01: "SSO whenever possible"). Safe only because
# Caddy is the sole way in: no host port since WP-55.
_SSO_GATED = (
    ("GET", re.compile(r"/shopping-list")),
    ("GET", re.compile(r"/v1/shopping-list")),
    ("POST", re.compile(r"/v1/shopping-list")),
    ("DELETE", re.compile(r"/v1/shopping-list/checked")),
    ("PATCH", re.compile(r"/v1/shopping-list/[0-9a-f]{32}")),
    ("DELETE", re.compile(r"/v1/shopping-list/[0-9a-f]{32}")),
    # WP-42.2: the Felt app (app_api.py), which also refuses any request
    # without Caddy's X-Authentik-Username header.
    ("GET", re.compile(r"/v1/app/me")),
    ("GET", re.compile(r"/v1/app/history")),
    ("GET", re.compile(r"/v1/app/asides")),
    ("GET", re.compile(r"/v1/app/margins")),
    ("GET", re.compile(r"/v1/app/margins/count")),
    ("POST", re.compile(r"/v1/app/margins/mark")),
    ("GET", re.compile(r"/v1/app/stream")),
    ("POST", re.compile(r"/v1/app/marks/seen")),
    ("POST", re.compile(r"/v1/app/chat")),
    # WP-42.5: the Systems page (systems_api.py)
    ("GET", re.compile(r"/v1/app/systems")),
    ("GET", re.compile(r"/v1/app/incidents/\d+")),
    ("POST", re.compile(r"/v1/app/incidents/\d+/handled")),
    # WP-42.5: the controls (controls_api.py)
    ("GET", re.compile(r"/v1/app/controls")),
    ("POST", re.compile(r"/v1/app/controls")),
    # WP-58's kill switch lives in the app's settings
    ("GET", re.compile(r"/v1/app/settings")),
    ("POST", re.compile(r"/v1/app/settings")),
)


def _sso_gated(method: str, path: str) -> bool:
    return any(method == m and rx.fullmatch(path) for m, rx in _SSO_GATED)


# WP-58: the only routes the responder's own token opens
_RESPONDER_ROUTES = (
    ("GET", re.compile(r"/v1/incidents/waiting")),
    ("POST", re.compile(r"/v1/incidents/(handoff|\d+/(report|local))")),
)


def _token_ok(given: str, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(given.encode(), token.encode())


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _origin_ok(request: Request, app_origins: tuple[str, ...]) -> bool:
    """WP-62 phase 4 CSRF guard for the SSO-gated (cookie/forward-auth) routes.
    The forward-auth cookie is SameSite=Lax and every *.clockmost.com app is
    same-site, so a sibling page could drive a credentialed mutation; these
    routes carry no bearer token and felt-core has no CSRF token. A safe method
    is always fine; an unsafe one is refused when the browser says the request
    is cross-origin (Origin set and not us, or Sec-Fetch-Site same-site/
    cross-site). A same-origin app request sends one of app_origins (the SSO
    host, or felt-tray's cert-only window host); native clients use the token
    path, not these routes, and send no Origin."""
    if request.method in _SAFE_METHODS:
        return True
    origin = request.headers.get("origin")
    if origin is not None and origin not in app_origins:
        return False
    site = request.headers.get("sec-fetch-site")
    return site is None or site in ("same-origin", "none")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """WP-55: with FELT_API_TOKEN set, every other route needs
    `Authorization: Bearer <token>` (constant-time compare) or gets 401."""
    config = load_config()
    token = config.api_token
    path = request.url.path
    # felt-ha-hub D3: Home Assistant's route takes only its Authentik token -
    # decided first, so no proxy identity header or other path can open it
    if ha_auth.is_ha_route(request.method, path):
        why = ha_auth.check(config, request.headers.get("authorization", ""))
        if why:
            logger.warning("ha agent refused on %s %s: %s", request.method, path, why)
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        request.state.ha_agent = True
        return await call_next(request)
    # WP-62 M2b B5.4 (D3): a Felt machine identity, set only by Caddy's
    # machine host (every other site strips it): per identity per route,
    # deny by default - checked before anything else, token or not.
    identity = request.headers.get("x-felt-identity") if config.machine_identity else None
    if identity:
        why = machine_auth.authorize(
            identity,
            request.headers.get("x-felt-serial", ""),
            request.method,
            path,
            config.machine_deny_serials,
        )
        if why:
            logger.warning(
                "machine caller %s refused on %s %s: %s", identity, request.method, path, why
            )
            return JSONResponse(status_code=403, content={"detail": why})
        return await call_next(request)
    if config.auth_mode == "identity":
        # B5.4: the shared tokens are retired - no Bearer path at all. Exempt
        # routes and Caddy's SSO-gated ones as before; anything else needed a
        # machine identity above, so it's refused here.
        if request.method == "OPTIONS" or path in _AUTH_EXEMPT:
            return await call_next(request)
        if _sso_gated(request.method, path):
            if not _origin_ok(request, (config.app_origin, config.app_desktop_origin)):
                return JSONResponse(status_code=403, content={"detail": "bad origin"})
            return await call_next(request)
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    if not token or request.method == "OPTIONS" or path in _AUTH_EXEMPT:
        return await call_next(request)
    if _sso_gated(request.method, path):
        # Caddy forward-auth is the authentication on these routes; felt-core
        # only has to stop a cross-origin (sibling-subdomain) CSRF here, since
        # they carry no token (WP-62 phase 4).
        if not _origin_ok(request, (config.app_origin, config.app_desktop_origin)):
            return JSONResponse(status_code=403, content={"detail": "bad origin"})
        return await call_next(request)
    given = request.headers.get("authorization", "")
    given = given[7:] if given.startswith("Bearer ") else ""
    if _token_ok(given, token):
        return await call_next(request)
    if any(request.method == m and rx.fullmatch(path) for m, rx in _RESPONDER_ROUTES) and _token_ok(
        given, config.responder_token
    ):
        return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"detail": "unauthorized"},
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.middleware("http")
async def latency_middleware(request: Request, call_next):
    """Times every /v1/* request end-to-end and stamps a standard
    Server-Timing header on the response, built from whatever brain.py/
    api.py stages recorded via metrics.timed() during the request (WP-04).
    Non-/v1/ paths (static assets, /openapi.json) aren't worth the
    request_id/SQLite overhead - and neither are the polling/status reads
    (_UNTIMED_PATHS): felt-tray polls /v1/music/now every 15 s, and those
    near-instant rows swamped the `total` stage's percentiles, hiding the
    real turn latency (audit 2026-09-24)."""
    if not request.url.path.startswith("/v1/") or _is_untimed(request.url.path):
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


@app.exception_handler(local_llm.AmbientSkipped)
async def ambient_skipped_handler(request: Request, exc: local_llm.AmbientSkipped):
    """An ambient line that gave way (llm_gate) isn't an outage: the react
    endpoints' "no reaction" answer - empty body, the reason in a header."""
    logger.info("%s: %s", request.url.path, exc)
    return Response(content=b"", media_type="audio/wav", headers={"X-Reaction-Skipped": "llm-gate"})


@app.exception_handler(local_llm.LocalLLMUnavailable)
async def felt_llm_offline_handler(request: Request, exc: local_llm.LocalLLMUnavailable):
    """felt-llm is the only model backend (WP-36 D''): when it's down every
    model-backed endpoint answers 503 and the client just skips the turn."""
    logger.warning("%s: felt-llm offline (%s)", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": "felt-llm offline"})


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


class ReactionTagRequest(BaseModel):
    text: str
    tags: list[str]


@app.post("/v1/reaction-tag")
async def reaction_tag_endpoint(req: ReactionTagRequest) -> dict:
    """WP-36 D'': pick the animation tag that best fits how Felt says a
    line. felt-animation sends its known tags (it owns the animation
    library); ONE schema-constrained felt-llm call (the tag is an enum of
    exactly those). `{"tag": null}` if the reply couldn't be parsed; 400
    for an empty tag list; 503 when felt-llm is down (the caller keeps its
    fallback tag)."""
    if not req.tags:
        raise HTTPException(status_code=400, detail="tags must not be empty")
    config = load_config()
    listed = ", ".join(req.tags)
    async with metrics.timed("reaction_tag", route="local", model=config.local_llm_model):
        reply = await local_llm.chat(
            [
                {
                    "role": "system",
                    "content": "You choose the animation tag for a desktop companion named Felt.",
                },
                {
                    "role": "user",
                    "content": (
                        f'Felt says: "{req.text}"\n\n'
                        "Pick the single animation tag that best fits how Felt says this line.\n"
                        f"Tags: {listed}"
                    ),
                },
            ],
            max_tokens=32,
            temperature=0,
            json_schema={
                "type": "object",
                "properties": {"tag": {"type": "string", "enum": req.tags}},
                "required": ["tag"],
            },
            config=config,
        )
    try:
        tag = json.loads(reply.text).get("tag")
    except (json.JSONDecodeError, AttributeError):
        return {"tag": None}
    return {"tag": tag if isinstance(tag, str) and tag in req.tags else None}


@app.get("/v1/history")
async def history_endpoint(session_id: str = "default", limit: int = 100) -> list[dict]:
    """A client's chat window is otherwise blank on every restart even
    though the conversation is already durable in SQLite (`get_history`
    was previously only read internally, to build the brain's own
    context) — this just exposes that same data so a dashboard can
    restore what was actually said."""
    config = load_config()
    return get_history(config.db_path, session_id, limit)


class _TTSResponse(Protocol):
    """What `_synthesize_reply` needs back from a TTS POST - narrower than
    `httpx.Response` so the unit tests' hand-written fake responses (no
    real network, see `tests/test_api_tts_cache.py`) satisfy the type
    without subclassing httpx."""

    @property
    def content(self) -> bytes: ...

    def raise_for_status(self) -> object: ...


class _TTSClient(Protocol):
    """What `_synthesize_reply` needs from its HTTP client - just enough
    of `httpx.AsyncClient`'s surface to also accept the tests' fake
    clients (`_ExplodingClient`/`FakeClient`), which duck-type `.post()`
    without subclassing `httpx.AsyncClient`."""

    async def post(self, url: str, *, json: dict[str, Any]) -> _TTSResponse: ...


async def _tts_audio(
    tts_text: str,
    emotion: str | None,
    config: Any,
    client: _TTSClient,
    *,
    first_chunk: bool = False,
) -> bytes:
    """One felt-voice /v1/tts call behind the shared WP-06 cache (keyed on
    text + emotion). Each real call is timed as a "tts" stage; a streamed
    reply's chunk 0 is additionally "tts_first_chunk" (WP-54) - it's the
    part the user actually waits for."""
    # WP-06: a cache hit here (warmed by brain.py's weather precompute
    # loop) means the common "what's the weather right now?" turn makes
    # zero calls to felt-voice at all.
    audio_bytes = tts_cache.get(tts_text, emotion)
    if audio_bytes is not None:
        return audio_bytes
    async with contextlib.AsyncExitStack() as stack:
        if first_chunk:
            await stack.enter_async_context(metrics.timed("tts_first_chunk", chars=len(tts_text)))
        await stack.enter_async_context(metrics.timed("tts", chars=len(tts_text)))
        tts_resp = await client.post(
            f"{config.voice_url}/v1/tts", json=tts_request_body(tts_text, emotion)
        )
        tts_resp.raise_for_status()
    audio_bytes = tts_resp.content
    tts_cache.put(tts_text, audio_bytes, emotion)
    return audio_bytes


async def _fill_reply_stream(stream: reply_stream.ReplyStream, config: Any) -> None:
    """Background half of a streamed reply: chunks 1..N-1, on a client of its
    own (the request's is closed as soon as chunk 0's response is sent)."""
    async with voice_client(config, timeout=60) as client:
        await reply_stream.run(stream, lambda _n, text: _tts_audio(text, None, config, client))


async def _synthesize_reply(
    reply: str,
    ui_action: dict[str, Any] | None,
    animation_tag: str | None,
    config: Any,
    client: _TTSClient,
    extra_headers: dict[str, str] | None = None,
    emotion: str | None = None,
    stream: bool = False,
) -> Response:
    """Shared by voice-chat and text-chat: TTS the reply and build the same
    audio/wav + X-* header response shape, so a client doesn't need to know
    or care which input mode produced it.

    `stream` (WP-54, only the two chat endpoints pass it): a reply that
    splits into 2+ sentence chunks answers with chunk 0's audio plus
    X-Reply-Stream/X-Reply-Chunks, and the rest is synthesised in the
    background for GET /v1/reply/{id}/chunk/{n}. A one-chunk reply is
    exactly the non-streamed response."""
    tts_text = strip_for_tts(reply)
    logger.debug("TTS input after strip_for_tts: %r", tts_text)

    chunks = (
        speech_chunks(tts_text, config.reply_stream_first_chunk_chars) if stream else [tts_text]
    )
    streaming = len(chunks) >= 2
    voice_down = False
    try:
        audio_bytes = await _tts_audio(
            chunks[0] if streaming else tts_text, emotion, config, client, first_chunk=streaming
        )
    except httpx.HTTPError:
        # felt-voice (the laptop) is off or failing: her reply still exists, so
        # it goes out as text only instead of a 500 that loses it (2026-10-15).
        logger.warning("felt-voice unavailable - replying without audio", exc_info=True)
        audio_bytes, streaming, voice_down = b"", False, True

    # Percent-encoded — headers must be ASCII-safe, a non-English reply
    # (accents, etc.) would otherwise break this.
    headers = {"X-Reply-Text": quote(reply), **(extra_headers or {})}
    if voice_down:
        headers["X-Voice"] = "unavailable"
    if streaming:
        live = reply_stream.create(chunks, audio_bytes)
        live.task = asyncio.create_task(_fill_reply_stream(live, config))
        headers["X-Reply-Stream"] = live.id
        headers["X-Reply-Chunks"] = str(len(chunks))
    if ui_action is not None:
        headers["X-UI-Action"] = quote(json.dumps(ui_action))
    if animation_tag is not None:
        headers["X-Animation-Tag"] = quote(animation_tag)

    return Response(content=audio_bytes, media_type="audio/wav", headers=headers)


def _wants_stream_v2(request: Request, config: Any) -> bool:
    """WP-65: a client that plays protocol v2 says so per request; every
    other client gets WP-54's whole-reply-then-chunks behaviour."""
    return config.reply_streaming and request.headers.get("x-accept-stream") == "2"


def _result_body(result: ChatResult) -> dict:
    return {
        "done": True,
        "text": result.reply,
        "ui_action": asdict(result.ui_action) if result.ui_action else None,
        "animation_tag": result.animation_tag,
        "emotion": result.emotion,
    }


async def _fill_open_stream(
    stream: reply_stream.ReplyStream,
    items: AsyncIterator[str | ReplyMeta | ChatResult],
    config: Any,
    on_result: Callable[[ChatResult], None] | None,
) -> None:
    """Background half of a v2 reply: each further chunk as chat_streamed()
    writes it, synthesised in order, then the finished result. felt-llm
    keeps writing while a chunk is in TTS (chat_streamed queues them)."""
    result: ChatResult | None = None
    try:
        async with voice_client(config, timeout=60) as client:
            async for item in items:
                if isinstance(item, ChatResult):
                    result = item
                    continue
                if isinstance(item, ReplyMeta):
                    continue  # went out with chunk 0 (or the result has it)
                tts_text = strip_for_tts(item)
                if not tts_text:
                    continue  # nothing to say out loud (an emoji line); the text has it
                n = stream.append(item)
                if stream.failed_from is not None:
                    continue
                try:
                    stream.set_chunk(n, await _tts_audio(tts_text, None, config, client))
                except Exception:
                    logger.exception("reply stream %s: TTS failed for chunk %d", stream.id, n)
                    stream.fail_from(n)
    except local_llm.LocalLLMUnavailable as e:
        logger.warning("reply stream %s: felt-llm went away mid-reply (%s)", stream.id, e)
    finally:
        if result is None:
            stream.end({"done": True, "text": " ".join(stream.texts), "error": "felt-llm offline"})
        else:
            stream.end(_result_body(result))
            if on_result is not None:
                on_result(result)


async def _reply_v2(
    message: str,
    session_id: str,
    tone: str | None,
    config: Any,
    client: _TTSClient,
    extra_headers: dict[str, str] | None = None,
    on_result: Callable[[ChatResult], None] | None = None,
) -> Response:
    """WP-65 protocol v2: chat_streamed(), and chunk 0's audio goes back the
    moment it exists - X-Reply-Text is chunk 0's text, X-Reply-Chunks
    "open", and the rest of the reply (its text, ui_action, tag) is at
    GET /v1/reply/{id} once done. A reply that never came out in chunks, or
    felt-voice being down, falls back to the whole-reply response."""
    items = chat_streamed(message, config, session_id, tone)
    first: str | ChatResult | None = None
    meta = ReplyMeta()
    async for item in items:
        if isinstance(item, ReplyMeta):
            meta = item  # the skill's hush / reaction: with chunk 0, not at the end
            continue
        if isinstance(item, ChatResult) or strip_for_tts(item):
            first = item
            break
    wav: bytes | None = None
    if isinstance(first, str):
        try:
            wav = await _tts_audio(strip_for_tts(first), None, config, client, first_chunk=True)
        except httpx.HTTPError:
            logger.warning("felt-voice unavailable - replying without audio", exc_info=True)
    if not isinstance(first, str) or wav is None:
        # the whole reply, the WP-54 way
        result = first if isinstance(first, ChatResult) else None
        async for item in items:
            if isinstance(item, ChatResult):
                result = item
        assert result is not None
        if on_result is not None:
            on_result(result)
        return await _synthesize_reply(
            result.reply,
            asdict(result.ui_action) if result.ui_action else None,
            result.animation_tag,
            config,
            client,
            extra_headers,
        )
    live = reply_stream.open_stream(first, wav)
    live.task = asyncio.create_task(_fill_open_stream(live, items, config, on_result))
    headers = {
        "X-Reply-Text": quote(first),
        "X-Reply-Stream": live.id,
        "X-Reply-Chunks": "open",
        "X-Reply-Stream-Version": "2",
        **(extra_headers or {}),
    }
    if meta.ui_action is not None:
        headers["X-UI-Action"] = quote(json.dumps(asdict(meta.ui_action)))
    if meta.animation_tag is not None:
        headers["X-Animation-Tag"] = quote(meta.animation_tag)
    return Response(content=wav, media_type="audio/wav", headers=headers)


@app.post("/v1/voice-chat")
async def voice_chat_endpoint(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = "default",
    tone: str | None = Form(None),
) -> Response:
    """One call: audio in, audio out. Orchestrates felt-voice (STT) ->
    the brain -> felt-voice (TTS), so a client doesn't have to."""
    config = load_config()
    if not config.voice_url:
        raise HTTPException(
            status_code=503, detail="Voice isn't configured (no FELT_VOICE_URL set)."
        )

    async with voice_client(config, timeout=60) as client:
        async with metrics.timed("stt"):
            stt_resp = await client.post(
                f"{config.voice_url}/v1/stt",
                files={"file": (file.filename, await file.read(), file.content_type)},
            )
            stt_resp.raise_for_status()
            transcribed = stt_resp.json()["text"]

        ui_action: dict[str, Any] | None = None
        animation_tag: str | None = None
        if transcribed.strip() and _wants_stream_v2(request, config):
            return await _reply_v2(
                transcribed,
                session_id,
                tone,
                config,
                client,
                {"X-Transcribed-Text": quote(transcribed)},
            )
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
            reply,
            ui_action,
            animation_tag,
            config,
            client,
            {"X-Transcribed-Text": quote(transcribed)},
            stream=config.reply_streaming,
        )


@app.post("/v1/text-chat")
async def text_chat_endpoint(req: ChatRequest, request: Request) -> Response:
    """Same shape as /v1/voice-chat's response (audio/wav + X-Reply-Text/
    X-Animation-Tag headers) but for already-typed text - so a typed
    message gets the same TTS + reaction-animation treatment a spoken one
    does, minus the STT step (Mario, 2026-09-21: "hook up the chat like it
    was voice input, need her to reply with TTS + animation")."""
    config = load_config()
    if not config.voice_url:
        raise HTTPException(
            status_code=503, detail="Voice isn't configured (no FELT_VOICE_URL set)."
        )

    if _wants_stream_v2(request, config):

        def marks(_result: ChatResult) -> None:
            if req.session_id == APP_SESSION:
                schedule_mark(config)  # WP-42.2: the tray window's sends get her marks too

        async with voice_client(config, timeout=60) as client:
            return await _reply_v2(
                req.message, req.session_id, req.tone, config, client, on_result=marks
            )

    result = await chat(req.message, config, req.session_id, req.tone)
    if req.session_id == APP_SESSION:
        schedule_mark(config)  # WP-42.2: the tray window's sends get her marks too
    async with voice_client(config, timeout=60) as client:
        return await _synthesize_reply(
            result.reply,
            asdict(result.ui_action) if result.ui_action else None,
            result.animation_tag,
            config,
            client,
            stream=config.reply_streaming,
        )


@app.get("/v1/reply/{stream_id}/chunk/{n}")
async def reply_chunk_endpoint(stream_id: str, n: int, timeout: float = 30.0) -> Response:
    """WP-54: long-polls for chunk n of a streamed reply (see
    _synthesize_reply). 200 + WAV + X-Chunk-Text once ready, 408 if it still
    isn't after `timeout` (clamped to 1-60 s), 204 for n past the last chunk
    (the stream is done), 404 for an unknown/expired id, 502 if that chunk's
    TTS failed."""
    stream = reply_stream.get(stream_id)
    if stream is None or n < 0:
        raise HTTPException(status_code=404, detail="unknown or expired stream")
    timeout = max(1.0, min(60.0, timeout))
    start = time.monotonic()
    # WP-65: an open stream may not have chunk n yet - 204 only once it ended
    if not await stream.wait_for_chunk_slot(n, timeout):
        if stream.done:
            return Response(status_code=204)
        raise HTTPException(status_code=408, detail="chunk not ready")
    try:
        wav = await stream.wait_chunk(n, max(0.0, timeout - (time.monotonic() - start)))
    except reply_stream.ChunkFailed:
        raise HTTPException(status_code=502, detail="chunk TTS failed") from None
    if wav is None:
        raise HTTPException(status_code=408, detail="chunk not ready")
    return Response(
        content=wav, media_type="audio/wav", headers={"X-Chunk-Text": quote(stream.texts[n])}
    )


@app.get("/v1/reply/{stream_id}")
async def reply_result_endpoint(stream_id: str) -> dict:
    """WP-65 (protocol v2): the finished reply behind a stream - `{done,
    text, ui_action, animation_tag, emotion}` (+ `error` if felt-llm went
    away mid-reply). Before it's done: `{done: false, text}` with the chunks
    so far. 404 for an unknown/expired id."""
    stream = reply_stream.get(stream_id)
    if stream is None:
        raise HTTPException(status_code=404, detail="unknown or expired stream")
    if not stream.done or stream.result is None:
        return {"done": False, "text": " ".join(stream.texts)}
    return stream.result


class ScreenContextRequest(BaseModel):
    description: str
    tone: str | None = None


@app.post("/v1/context/screen")
async def screen_context_endpoint(
    req: ScreenContextRequest, session_id: str = "default"
) -> Response:
    """Receives passive screen observations from felt-vision.
    Returns audio+animation headers just like chat, but handles pure-silent
    reactions by skipping TTS entirely. 503 when felt-llm is down."""
    config = load_config()
    result = await process_screen_context(req.description, config, session_id, req.tone)

    if not result.reply.strip():
        # Pure silent body-language reaction
        headers = {}
        if result.animation_tag is not None:
            headers["X-Animation-Tag"] = quote(result.animation_tag)
        return Response(content=b"", media_type="audio/wav", headers=headers)

    async with voice_client(config, timeout=60) as client:
        return await _synthesize_reply(
            result.reply,
            asdict(result.ui_action) if result.ui_action else None,
            result.animation_tag,
            config,
            client,
        )


@app.post("/v1/react/screen")
async def react_screen_endpoint(
    image: UploadFile | None = File(None),
    app_name: str = Form(..., alias="app"),
    title: str = Form(""),
    trigger: str = Form("focus"),
    session_id: str = "dashboard",
    tone: str | None = Form(None),
    seen: str | None = Form(None),
) -> Response:
    """WP-07: felt-tray's new per-window capture path - the JPEG has
    already cleared the blocklist client-side before this is ever called.
    A single multimodal felt-llm call - 503 {"detail": "felt-llm offline"}
    when it's unreachable, which felt-tray treats as "no reaction".

    cm-qplv: with its [vision] table set, felt-tray describes the screen on
    the desktop's own GPU and sends that text as `seen` instead of the image."""
    if image is None and not (seen and seen.strip()):
        raise HTTPException(status_code=422, detail="send an image or seen")
    config = load_config()
    jpeg_bytes = await image.read() if image is not None else b""
    seen = seen.strip() if seen and seen.strip() else None
    # "look at this" (manual) is him asking: chat; the rest is ambient
    gate = (
        contextlib.nullcontext()
        if trigger == "manual"
        else llm_gate.ambient("poe" if poe_reactions.is_poe(app_name) else "screen")
    )
    with gate:
        result = await react_to_screen(
            jpeg_bytes,
            app_name,
            title,
            config,
            session_id=session_id,
            trigger=trigger,
            tone=tone,
            seen=seen,
        )

    emotion_headers = {"X-Emotion": quote(result.emotion)} if result.emotion else {}

    if not result.reply.strip():
        headers = dict(emotion_headers)
        if result.animation_tag is not None:
            headers["X-Animation-Tag"] = quote(result.animation_tag)
        return Response(content=b"", media_type="audio/wav", headers=headers)

    async with voice_client(config, timeout=60) as client:
        return await _synthesize_reply(
            result.reply,
            asdict(result.ui_action) if result.ui_action else None,
            result.animation_tag,
            config,
            client,
            extra_headers=emotion_headers or None,
            emotion=result.emotion,
        )


class EventReactionRequest(BaseModel):
    event: str
    details: dict[str, Any] = {}
    # WP-15 tone line, same optional field the screen endpoints take.
    tone: str | None = None


@app.post("/v1/react/event")
async def react_event_endpoint(
    req: EventReactionRequest, session_id: str = "dashboard"
) -> Response:
    """A short spoken reaction to a desktop event (events.py) - e.g.
    {"event": "music_new_track", "details": {"title", "artist", "album"}}.

    Same response contract as /v1/react/screen, so clients reuse their
    handling: 200 audio/wav with X-Reply-Text (percent-encoded) and
    X-Emotion when she speaks; 200 with an EMPTY body (and no
    X-Reply-Text) when she doesn't - cooldown, dedupe, unknown event, too
    short an absence - plus an additive X-Reaction-Skipped: <reason>
    header saying why. 503 {"detail": "voice_unavailable"} only if TTS
    isn't configured/reachable, 503 {"detail": "felt-llm offline"} when
    felt-llm is down. Never stored as a screen observation."""
    config = load_config()
    if not config.voice_url:
        return JSONResponse(status_code=503, content={"detail": "voice_unavailable"})

    if not features.enabled(config.db_path, "ambient.desktop_events"):
        return Response(
            content=b"", media_type="audio/wav", headers={"X-Reaction-Skipped": "switched-off"}
        )
    with llm_gate.ambient(f"event {req.event}"):
        result = await react_to_event(
            req.event, req.details, config, session_id=session_id, tone=req.tone
        )
    if not result.line:
        return Response(
            content=b"",
            media_type="audio/wav",
            headers={"X-Reaction-Skipped": quote(result.skipped or "none")},
        )

    emotion_headers = {"X-Emotion": quote(result.emotion)} if result.emotion else {}
    try:
        async with voice_client(config, timeout=60) as client:
            return await _synthesize_reply(
                result.line,
                None,
                None,
                config,
                client,
                extra_headers=emotion_headers or None,
                emotion=result.emotion,
            )
    except httpx.HTTPError:
        logger.exception("event reaction: TTS failed for %r", result.line)
        return JSONResponse(status_code=503, content={"detail": "voice_unavailable"})


class EventLogRequest(BaseModel):
    kind: str
    data: dict[str, Any] = {}
    at: str | None = None


@app.post("/v1/events", status_code=204)
async def log_event_endpoint(req: EventLogRequest, background_tasks: BackgroundTasks) -> Response:
    """WP-26: felt-animation's SILENT desktop-event log - game_start/stop,
    afk_start/end, day_start, music_track, mood, window_fall, prop_used,
    hush, ledge_sit, ... Any kind is accepted (an unrecognized one is
    logged but contributes nothing to the diary - see diary._event_line);
    `data` is size-capped, never rejected. No reaction, no TTS - distinct
    from /v1/react/event (events.py), which is a SPOKEN reaction. Also the
    WP-26 diary trigger: day_start/afk_end after 05:00 local writes
    yesterday's entry. It runs as a FastAPI background task: after the 204
    is sent (the model write takes seconds, and felt-animation's post
    timeout is 2 s), but still on the server's event loop, so proactive
    pre-synthesis (WP-25) works."""
    config = load_config()
    at_utc = diary.normalize_at(req.at)
    save_event(config.db_path, req.kind[:64], diary.cap_event_data(req.data), at_utc)
    background_tasks.add_task(diary.on_event, config, req.kind, at_utc)
    return Response(status_code=204)


@app.get("/v1/diary")
async def diary_list_endpoint(limit: int = 30, date: str | None = None) -> dict:
    """LAN-only, like the rest of felt-core's API (felt-dashboard's "Felt's
    diary" page, WP-26). `date=` returns just that one entry (or an empty
    `entries` list); otherwise the most recent `limit` entries, newest
    first, plus the weekly summaries."""
    config = load_config()
    if date is not None:
        row = get_diary(config.db_path, date)
        return {"entries": [row] if row else [], "weeks": []}
    return {
        "entries": get_diary_list(config.db_path, limit=limit),
        "weeks": get_diary_weeks(config.db_path),
    }


class DiaryPatchRequest(BaseModel):
    entry: str


@app.patch("/v1/diary/{date}")
async def diary_patch_endpoint(date: str, req: DiaryPatchRequest) -> dict:
    """Mario correcting her memory (WP-26: "Decided ... Mario can correct
    her memory"). Marks the row edited. Deliberately CLEARS callbacks_json
    rather than spending a second model call regenerating them from the
    new text - a hand-edited entry is usually a small correction, and a
    stale callback pointing at wording that's no longer there is worse
    than no callback until the next nightly write naturally adds fresh
    ones. Documented here since the WP left this as an implementer call."""
    config = load_config()
    updated = update_diary_entry(config.db_path, date, req.entry, "[]")
    if not updated:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@app.delete("/v1/diary/{date}")
async def diary_delete_endpoint(date: str) -> dict:
    config = load_config()
    return {"deleted": delete_diary(config.db_path, date)}


@app.post("/v1/diary/write")
async def diary_write_debug_endpoint(date: str) -> dict:
    """A manual trigger for testing (WP-26), gated by FELT_DEBUG the same
    way /v1/proactive/debug is - not a normal client path."""
    config = load_config()
    if not config.debug:
        raise HTTPException(status_code=404, detail="not found")
    written = await diary.write_diary_for_date(config, date)
    return {"written": written}


@app.get("/v1/self-memory")
async def self_memory_list_endpoint(status: str | None = None) -> list[dict]:
    config = load_config()
    return get_self_memories(config.db_path, status=status)


class SelfMemoryPatchRequest(BaseModel):
    text: str | None = None
    pinned: bool | None = None
    status: str | None = None


@app.patch("/v1/self-memory/{memory_id}")
async def self_memory_patch_endpoint(memory_id: int, req: SelfMemoryPatchRequest) -> dict:
    """Mario's view/pin/edit/veto controls (WP-26 combined with personality
    plan P4, Decision 3). `status: "vetoed"` means never re-added - its
    text feeds the nightly writer's "never" list (diary._build_writer_prompt)
    for as long as the vetoed row exists."""
    config = load_config()
    if get_self_memory(config.db_path, memory_id) is None:
        raise HTTPException(status_code=404, detail="not found")
    updated = update_self_memory(
        config.db_path, memory_id, text=req.text, pinned=req.pinned, status=req.status
    )
    return {"ok": updated}


@app.delete("/v1/self-memory/{memory_id}")
async def self_memory_delete_endpoint(memory_id: int) -> dict:
    config = load_config()
    return {"deleted": delete_self_memory(config.db_path, memory_id)}


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


@app.get("/v1/weather/now")
async def weather_now_endpoint() -> dict:
    """WP-21: the current weather condition from the WP-06 precompute cache
    (refreshed every 15 min, stale after 20) - felt-animation polls it for
    weather props. `condition` is "rain" / "sun" / "snow" / "storm" or None."""
    return current_weather()


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


# HA's item uids (shopping.py) - "checked" never matches, so the clear route stays apart
_ITEM_ID = r"[0-9a-f]{32}"


@app.exception_handler(shopping.ShoppingUnavailable)
async def shopping_unavailable_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "Home Assistant didn't answer"})


@app.get("/v1/shopping-list")
async def get_shopping_list_endpoint() -> list[dict]:
    return await shopping.items(load_config())


@app.post("/v1/shopping-list")
async def add_shopping_list_item_endpoint(req: ShoppingItemRequest) -> dict:
    await shopping.add(load_config(), req.text)
    return {"ok": True}


@app.delete("/v1/shopping-list/checked")
async def clear_checked_shopping_list_endpoint() -> dict:
    await shopping.clear_checked(load_config())
    return {"ok": True}


@app.patch("/v1/shopping-list/{item_id}")
async def set_shopping_item_checked_endpoint(
    checked: bool, item_id: str = FastAPIPath(pattern=f"^{_ITEM_ID}$")
) -> dict:
    await shopping.set_checked(load_config(), item_id, checked)
    return {"ok": True}


@app.delete("/v1/shopping-list/{item_id}")
async def delete_shopping_item_endpoint(
    item_id: str = FastAPIPath(pattern=f"^{_ITEM_ID}$"),
) -> dict:
    await shopping.remove(load_config(), item_id)
    return {"ok": True}


@app.get("/shopping-list")
async def shopping_list_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "shopping-list.html")


# -- WP-25: the proactive channel (felt-core -> desktop) --------------------


@app.get("/v1/proactive/next")
async def proactive_next_endpoint(timeout: float = 25.0) -> Response:
    """felt-tray's long-poll. Returns the oldest undelivered, unexpired
    item (already marked delivered) as soon as one exists, or an empty
    204 once the (clamped) timeout elapses with nothing to hand out."""
    config = load_config()
    item = await wait_for_next(config, clamp_timeout(timeout))
    if item is None:
        return Response(status_code=204)
    return JSONResponse(content=item)


@app.get("/v1/proactive/{item_id}/audio")
async def proactive_audio_endpoint(item_id: int) -> Response:
    """The item's TTS WAV, fetched separately from /next so a client can
    show/queue the item before audio is ready - reuses the same WP-06
    cache + felt-voice call /v1/voice-chat and friends already share via
    tts_cache/_synthesize_reply, just without the X-* reply headers (there's
    no ui_action/animation_tag to carry here beyond what /next already
    returned)."""
    config = load_config()
    row = get_proactive_item(config.db_path, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if not config.voice_url:
        raise HTTPException(status_code=503, detail="voice_unavailable")

    # Pre-synthesised at enqueue time where possible (proactive.py).
    emotion = json.loads(row["payload_json"] or "{}").get("emotion")
    try:
        audio_bytes = await proactive_audio(config, row["text"], emotion)
    except httpx.HTTPError as e:
        # felt-voice runs on the laptop, which is often off: the same answer
        # as no voice configured, not a 500 (2026-10-18 logs)
        logger.warning("proactive audio: felt-voice unavailable (%r)", e)
        raise HTTPException(status_code=503, detail="voice_unavailable") from e
    return Response(content=audio_bytes, media_type="audio/wav")


@app.post("/v1/proactive/{item_id}/requeue")
async def proactive_requeue_endpoint(item_id: int) -> dict:
    """felt-tray calls this when Felt was hidden/away and the item wasn't
    `urgent` - don't lose it, hand it out again on the next /next poll."""
    config = load_config()
    return {"requeued": requeue_proactive(config.db_path, item_id)}


class ProactiveDebugRequest(BaseModel):
    kind: str = "debug"
    text: str
    animation_tag: str | None = None
    emotion: str | None = None
    ttl_s: float = 600.0


@app.post("/v1/proactive/debug")
async def proactive_debug_endpoint(req: ProactiveDebugRequest) -> dict:
    """WP-25 accept test's door into the queue - only reachable with
    FELT_DEBUG=1, a 404 (not just an auth error) otherwise so its
    existence isn't even discoverable on a normal deployment."""
    config = load_config()
    if not config.debug:
        raise HTTPException(status_code=404, detail="not found")
    item_id = enqueue_proactive(
        config,
        req.kind,
        req.text,
        animation_tag=req.animation_tag,
        emotion=req.emotion,
        ttl_s=req.ttl_s,
    )
    return {"id": item_id}


def _ws_authorized(websocket: WebSocket) -> bool:
    """WP-55: the HTTP auth middleware doesn't see websockets, and browsers
    can't set headers on them - so a Bearer header or ?token= is checked here."""
    config = load_config()
    if config.auth_mode == "identity":
        return False  # B5.4: no shared token any more, and no machine caller uses websockets
    token = config.api_token
    if not token:
        return True
    given = websocket.headers.get("authorization", "")
    given = given[7:] if given.startswith("Bearer ") else websocket.query_params.get("token", "")
    return hmac.compare_digest(given.encode(), token.encode())


@app.websocket("/v1/chat/ws")
async def chat_ws(websocket: WebSocket) -> None:
    if not _ws_authorized(websocket):
        await websocket.close(code=1008)  # policy violation, before accept
        return
    await websocket.accept()
    config = load_config()
    try:
        while True:
            data = await websocket.receive_json()
            try:
                result = await chat(data["message"], config, data.get("session_id", "default"))
            except local_llm.LocalLLMUnavailable:
                # Same contract as the HTTP 503: the socket stays open.
                await websocket.send_json({"error": "felt-llm offline"})
                continue
            await websocket.send_json(
                {
                    "reply": result.reply,
                    "ui_action": asdict(result.ui_action) if result.ui_action else None,
                    "animation_tag": result.animation_tag,
                }
            )
    except WebSocketDisconnect:
        # The ordinary way this loop ends - the client closed the tab/app.
        # Nothing to log beyond that it happened; the socket is already
        # gone, so there's no `websocket.close()` to call here either.
        logger.info("chat_ws: client disconnected")
    except Exception:
        # WP-40: this used to swallow any error and just close the socket
        # with zero logging - the single worst offender across ~50+ broad
        # excepts audited (ENGINEERING-RULES.md rule 33/34). Now logged
        # with what was actually happening (the /v1/chat/ws message loop)
        # before still closing the socket the same way as before.
        logger.exception("chat_ws: message loop failed, closing the socket")
        await websocket.close()


def run() -> None:
    # No logging config existed anywhere in this codebase - felt_core's own
    # logger.info() calls (e.g. brain.py's chat routing)
    # were silently dropped below the default WARNING threshold. Only
    # uvicorn's own access-log lines were ever visible.
    logging.basicConfig(level=logging.INFO)
    # httpx logs every request URL at INFO; keep URLs (and anything a caller
    # ever puts in a query string) out of the logs (2026-09-27 key leak).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = load_config()
    uvicorn.run(app, host=config.host, port=config.port)
