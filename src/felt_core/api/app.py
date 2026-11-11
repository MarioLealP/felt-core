"""The frontend API — what a dashboard, voice pipeline, or Android client actually talks to.

None of them touch brain internals directly.

v1, deliberately simple: one request in, one reply out, no session/
conversation-history handling yet (that's SQLite persistence, a separate
near-term addition — see assistant-core.md's Build phases; this phase is
just about having *an* API surface at all, versioned so it can evolve).
"""

import asyncio
import contextlib
import logging
import time
import uuid

import uvicorn
from fastapi import (
    FastAPI,
    Request,
)
from fastapi.responses import JSONResponse, Response

from felt_core.api import ha_auth, machine_auth
from felt_core.api.app_api import router as app_router
from felt_core.api.auth import _AUTH_EXEMPT, _RESPONDER_ROUTES, _origin_ok, _sso_gated, _token_ok
from felt_core.api.calendar_api import router as calendar_router
from felt_core.api.chat import router as chat_router
from felt_core.api.controls_api import router as controls_router
from felt_core.api.diary import router as diary_router
from felt_core.api.ha_api import router as ha_router
from felt_core.api.incident_api import router as incident_router
from felt_core.api.metrics import router as metrics_router
from felt_core.api.music import router as music_router
from felt_core.api.notices_api import router as notices_router
from felt_core.api.proactive import router as proactive_router
from felt_core.api.react import router as react_router
from felt_core.api.reply import router as reply_router
from felt_core.api.self_memory import router as self_memory_router
from felt_core.api.settings import router as settings_router
from felt_core.api.shopping import router as shopping_router
from felt_core.api.systems_api import router as systems_router
from felt_core.api.weather import router as weather_router
from felt_core.brain import (
    llm_warmup_loop,
    music_taste_loop,
    router,
    weather_precompute_loop,
)
from felt_core.clients import local_llm, shopping
from felt_core.clients.poe_trade_cache import watch as poe_trade_cache_watch
from felt_core.config import FeltConfig, load_config, reveal
from felt_core.core import incidents, metrics
from felt_core.core.alarms import reschedule_pending_alarms
from felt_core.core.calendar_headsup import calendar_headsup_loop
from felt_core.core.emails import watch as email_watch
from felt_core.core.ender import watch as ender_watch
from felt_core.core.ntfy_listener import listen as ntfy_listen
from felt_core.db.backup import backup_loop
from felt_core.db.latency import delete_old_latency_rows
from felt_core.db.proactive import prune_expired_proactive

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the background watchers and loops, and cancel them on shutdown."""
    config = load_config()
    # WP-62 M1: say whether the vault path delivered (never the value itself)
    if config.vault_probe:
        logger.info("vault: probe delivered (%d chars)", len(reveal(config.vault_probe)))
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
    # a consistent felt.db copy for restic, which copies the live file raw
    backup_task = asyncio.create_task(backup_loop(config.db_path))
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
    try:
        yield
    finally:  # also when the app stops on an exception
        tasks = (
            ntfy_task,
            email_task,
            poe_cache_task,
            weather_task,
            music_taste_task,
            router_task,
            warmup_task,
            calendar_task,
            incidents_task,
            ender_task,
            backup_task,
        )
        for task in tasks:
            task.cancel()
        # wait for them: a task cancelled mid-query closes its DB connection on the way out, and that needs
        # the loop still running (left to the loop's own shutdown, a second cancel hit the close and it hung)
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="felt-core", version="0.1.0", lifespan=lifespan)

app.include_router(app_router, prefix="/v1/app")
app.include_router(app_router, prefix="/v1/ha/app")
app.include_router(incident_router)
app.include_router(systems_router)
app.include_router(notices_router)
app.include_router(calendar_router)
app.include_router(controls_router)
app.include_router(ha_router)
app.include_router(metrics_router)
app.include_router(chat_router)
app.include_router(reply_router)
app.include_router(react_router)
app.include_router(diary_router)
app.include_router(self_memory_router)
app.include_router(settings_router)
app.include_router(weather_router)
app.include_router(music_router)
app.include_router(shopping_router)
app.include_router(proactive_router)

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


def _ha_refusal(request: Request, config: FeltConfig) -> JSONResponse | None:
    """Home Assistant's route takes only its Authentik token (felt-ha-hub D3)."""
    why = ha_auth.check(config, request.headers.get("authorization", ""))
    if why:
        logger.warning("ha agent refused on %s %s: %s", request.method, request.url.path, why)
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    request.state.ha_agent = True
    return None


def _machine_refusal(request: Request, config: FeltConfig, identity: str) -> JSONResponse | None:
    """A Felt machine identity: allowed per identity per route, denied by default (WP-62 M2b B5.4, D3)."""
    path = request.url.path
    why = machine_auth.authorize(
        identity,
        request.headers.get("x-felt-serial", ""),
        request.method,
        path,
        config.machine_deny_serials,
    )
    if why:
        logger.warning("machine caller %s refused on %s %s: %s", identity, request.method, path, why)
        return JSONResponse(status_code=403, content={"detail": why})
    return None


def _sso_refusal(request: Request, config: FeltConfig) -> JSONResponse | None:
    """Caddy's SSO-gated routes: forward-auth did the authentication, felt-core only stops cross-origin CSRF.

    They carry no token, so a sibling-subdomain page could otherwise call them (WP-62 phase 4).
    """
    if not _origin_ok(request, (config.app_origin, config.app_desktop_origin)):
        return JSONResponse(status_code=403, content={"detail": "bad origin"})
    return None


def _identity_mode_refusal(request: Request, config: FeltConfig) -> JSONResponse | None:
    """B5.4: the shared tokens are retired - no Bearer path at all.

    Exempt routes and Caddy's SSO-gated ones as before; anything else needed a
    machine identity, so it's refused here.
    """
    path = request.url.path
    if request.method == "OPTIONS" or path in _AUTH_EXEMPT:
        return None
    if _sso_gated(request.method, path):
        return _sso_refusal(request, config)
    return JSONResponse(status_code=401, content={"detail": "unauthorized"})


def _token_refusal(request: Request, config: FeltConfig) -> JSONResponse | None:
    """The bearer token (constant-time compare), or the responder's own token on its routes."""
    token = reveal(config.api_token)
    path = request.url.path
    if not token or request.method == "OPTIONS" or path in _AUTH_EXEMPT:
        return None
    if _sso_gated(request.method, path):
        return _sso_refusal(request, config)
    given = request.headers.get("authorization", "")
    given = given[7:] if given.startswith("Bearer ") else ""
    if _token_ok(given, token):
        return None
    if any(request.method == m and rx.fullmatch(path) for m, rx in _RESPONDER_ROUTES) and _token_ok(
        given, reveal(config.responder_token)
    ):
        return None
    return JSONResponse(
        status_code=401,
        content={"detail": "unauthorized"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _refusal(request: Request, config: FeltConfig) -> JSONResponse | None:
    """The response that refuses this request, or None to let it through - first matching regime decides."""
    # felt-ha-hub D3: decided first, so no proxy identity header or other path can open it
    if ha_auth.is_ha_route(request.method, request.url.path):
        return _ha_refusal(request, config)
    # WP-62 M2b B5.4: set only by Caddy's machine host (every other site strips it),
    # checked before anything else, token or not
    identity = request.headers.get("x-felt-identity") if config.machine_identity else None
    if identity:
        return _machine_refusal(request, config, identity)
    if config.auth_mode == "identity":
        return _identity_mode_refusal(request, config)
    return _token_refusal(request, config)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """WP-55: the auth check on every route but the exempt ones (rules in _refusal)."""
    refused = _refusal(request, load_config())
    if refused is not None:
        return refused
    return await call_next(request)


@app.middleware("http")
async def latency_middleware(request: Request, call_next):
    """Times every /v1/* request end-to-end and stamps a standard Server-Timing header on the response.

    Built from whatever brain.py/api.py stages recorded via metrics.timed()
    during the request (WP-04). Non-/v1/ paths (static assets, /openapi.json) aren't worth the
    request_id/SQLite overhead - and neither are the polling/status reads
    (_UNTIMED_PATHS): felt-tray polls /v1/music/now every 15 s, and those
    near-instant rows swamped the `total` stage's percentiles, hiding the
    real turn latency (audit 2026-09-24).
    """
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


@app.exception_handler(local_llm.AmbientSkippedError)
async def ambient_skipped_handler(request: Request, exc: local_llm.AmbientSkippedError):  # ruff: ignore[unused-async]  # async by contract
    """An ambient line that gave way (llm_gate) isn't an outage.

    The react endpoints' "no reaction" answer - empty body, the reason in a header.
    """
    logger.info("%s: %s", request.url.path, exc)
    return Response(content=b"", media_type="audio/wav", headers={"X-Reaction-Skipped": "llm-gate"})


@app.exception_handler(local_llm.LocalLLMUnavailableError)
async def felt_llm_offline_handler(request: Request, exc: local_llm.LocalLLMUnavailableError):  # ruff: ignore[unused-async]  # async by contract
    """felt-llm is the only model backend (WP-36 D'').

    When it's down every model-backed endpoint answers 503 and the client
    just skips the turn.
    """
    logger.warning("%s: felt-llm offline (%s)", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": "felt-llm offline"})


@app.exception_handler(shopping.ShoppingUnavailableError)
async def shopping_unavailable_handler(_request: Request, _exc: Exception) -> JSONResponse:  # ruff: ignore[unused-async]  # async by contract
    """Answer 503 when Home Assistant didn't answer a shopping call."""
    return JSONResponse(status_code=503, content={"detail": "Home Assistant didn't answer"})


def run() -> None:
    """Configure logging and serve the felt-core API with uvicorn."""
    # No logging config existed anywhere in this codebase - felt_core's own
    # logger.info() calls (e.g. brain.py's chat routing)
    # were silently dropped below the default WARNING threshold. Only
    # uvicorn's own access-log lines were ever visible.
    logging.basicConfig(level=logging.INFO)
    # httpx2 logs every request URL at INFO; keep URLs (and anything a caller
    # ever puts in a query string) out of the logs (2026-09-27 key leak).
    # Plain httpx too: huggingface-hub (fastembed's model download) still uses it.
    for name in ("httpx2", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)
    config = load_config()
    uvicorn.run(app, host=config.host, port=config.port)
