"""Reactions: screen context, screen and event reactions (/v1/context, /v1/react/...) and the event log."""

import contextlib
import logging
from typing import Annotated, Any
from urllib.parse import quote

import httpx2
from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import felt_core.core.diary_material as diary_material
from felt_core.api.reply import _synthesize_reply
from felt_core.brain import (
    poe_reactions,
    process_screen_context,
    react_to_screen,
)
from felt_core.clients.felt_tls import voice_client
from felt_core.config import load_config
from felt_core.core import diary, features, llm_gate
from felt_core.core.events import react_to_event
from felt_core.db.events import save_event

logger = logging.getLogger(__name__)
router = APIRouter()


class ScreenContextRequest(BaseModel):
    """A passive screen description from felt-vision and optional tone."""

    description: str
    tone: str | None = None


@router.post("/v1/context/screen")
async def screen_context_endpoint(req: ScreenContextRequest, session_id: str = "default") -> Response:
    """Receives passive screen observations from felt-vision.

    Returns audio+animation headers just like chat, but handles pure-silent
    reactions by skipping TTS entirely. 503 when felt-llm is down.
    """
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
            result.ui_action.model_dump() if result.ui_action else None,
            result.animation_tag,
            config,
            client,
        )


@router.post("/v1/react/screen")
async def react_screen_endpoint(
    app_name: Annotated[str, Form(alias="app")],
    image: Annotated[UploadFile | None, File()] = None,
    title: Annotated[str, Form()] = "",
    trigger: Annotated[str, Form()] = "focus",
    session_id: str = "dashboard",
    tone: Annotated[str | None, Form()] = None,
    seen: Annotated[str | None, Form()] = None,
) -> Response:
    """WP-07: felt-tray's new per-window capture path.

    The JPEG has already cleared the blocklist client-side before this is
    ever called. A single multimodal felt-llm call - 503 {"detail":
    "felt-llm offline"} when it's unreachable, which felt-tray treats as
    "no reaction".

    cm-qplv: with its [vision] table set, felt-tray describes the screen on
    the desktop's own GPU and sends that text as `seen` instead of the image.
    """
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
            result.ui_action.model_dump() if result.ui_action else None,
            result.animation_tag,
            config,
            client,
            extra_headers=emotion_headers or None,
            emotion=result.emotion,
        )


class EventReactionRequest(BaseModel):
    """A desktop event and its details for a spoken reaction."""

    event: str
    details: dict[str, Any] = {}
    # WP-15 tone line, same optional field the screen endpoints take.
    tone: str | None = None


@router.post("/v1/react/event")
async def react_event_endpoint(req: EventReactionRequest, session_id: str = "dashboard") -> Response:
    """A short spoken reaction to a desktop event (events.py).

    e.g. {"event": "music_new_track", "details": {"title", "artist", "album"}}.

    Same response contract as /v1/react/screen, so clients reuse their
    handling: 200 audio/wav with X-Reply-Text (percent-encoded) and
    X-Emotion when she speaks; 200 with an EMPTY body (and no
    X-Reply-Text) when she doesn't - cooldown, dedupe, unknown event, too
    short an absence - plus an additive X-Reaction-Skipped: <reason>
    header saying why. 503 {"detail": "voice_unavailable"} only if TTS
    isn't configured/reachable, 503 {"detail": "felt-llm offline"} when
    felt-llm is down. Never stored as a screen observation.
    """
    config = load_config()
    if not config.voice_url:
        return JSONResponse(status_code=503, content={"detail": "voice_unavailable"})

    if not features.enabled(config.db_path, "ambient.desktop_events"):
        return Response(content=b"", media_type="audio/wav", headers={"X-Reaction-Skipped": "switched-off"})
    with llm_gate.ambient(f"event {req.event}"):
        result = await react_to_event(req.event, req.details, config, session_id=session_id, tone=req.tone)
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
    except httpx2.HTTPError:
        logger.exception("event reaction: TTS failed for %r", result.line)
        return JSONResponse(status_code=503, content={"detail": "voice_unavailable"})


class EventLogRequest(BaseModel):
    """A silent desktop event kind, its data and optional timestamp."""

    kind: str
    data: dict[str, Any] = {}
    at: str | None = None


@router.post("/v1/events", status_code=204)
async def log_event_endpoint(req: EventLogRequest, background_tasks: BackgroundTasks) -> Response:
    """WP-26: felt-animation's SILENT desktop-event log.

    game_start/stop, afk_start/end, day_start, music_track, mood,
    window_fall, prop_used, hush, ledge_sit, ... Any kind is accepted (an
    unrecognized one is logged but contributes nothing to the diary - see
    diary._event_line); `data` is size-capped, never rejected. No reaction,
    no TTS - distinct
    from /v1/react/event (events.py), which is a SPOKEN reaction. Also the
    WP-26 diary trigger: day_start/afk_end after 05:00 local writes
    yesterday's entry. It runs as a FastAPI background task: after the 204
    is sent (the model write takes seconds, and felt-animation's post
    timeout is 2 s), but still on the server's event loop, so proactive
    pre-synthesis (WP-25) works.
    """
    config = load_config()
    at_utc = diary_material.normalize_at(req.at)
    save_event(config.db_path, req.kind[:64], diary_material.cap_event_data(req.data), at_utc)
    background_tasks.add_task(diary.on_event, config, req.kind, at_utc)
    return Response(status_code=204)
