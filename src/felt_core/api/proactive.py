"""Her pushes: /v1/proactive/... (WP-25: the proactive channel, felt-core -> desktop)."""

import json
import logging

import httpx2
from fastapi import (
    APIRouter,
    HTTPException,
)
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from felt_core.config import load_config
from felt_core.core.proactive import clamp_timeout, enqueue_proactive, proactive_audio, wait_for_next
from felt_core.db.proactive import get_proactive_item, requeue_proactive

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/v1/proactive/next")
async def proactive_next_endpoint(timeout: float = 25.0) -> Response:
    """felt-tray's long-poll.

    Returns the oldest undelivered, unexpired item (already marked delivered) as
    soon as one exists, or an empty 204 once the (clamped) timeout elapses with
    nothing to hand out.
    """
    config = load_config()
    item = await wait_for_next(config, clamp_timeout(timeout))
    if item is None:
        return Response(status_code=204)
    return JSONResponse(content=item)


@router.get("/v1/proactive/{item_id}/audio")
async def proactive_audio_endpoint(item_id: int) -> Response:
    """The item's TTS WAV, fetched separately from /next.

    so a client can show/queue the item before audio is ready - reuses the same WP-06
    cache + felt-voice call /v1/voice-chat and friends already share via
    tts_cache/_synthesize_reply, just without the X-* reply headers (there's
    no ui_action/animation_tag to carry here beyond what /next already
    returned).
    """
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
    except httpx2.HTTPError as e:
        # felt-voice runs on the laptop, which is often off: the same answer
        # as no voice configured, not a 500 (2026-10-18 logs)
        logger.warning("proactive audio: felt-voice unavailable (%r)", e)
        raise HTTPException(status_code=503, detail="voice_unavailable") from e
    return Response(content=audio_bytes, media_type="audio/wav")


@router.post("/v1/proactive/{item_id}/requeue")
async def proactive_requeue_endpoint(item_id: int) -> dict:
    """felt-tray calls this when Felt was hidden/away and the item wasn't `urgent`.

    don't lose it, hand it out again on the next /next poll.
    """
    config = load_config()
    return {"requeued": requeue_proactive(config.db_path, item_id)}


class ProactiveDebugRequest(BaseModel):
    """The fields for one test item pushed through /v1/proactive/debug."""

    kind: str = "debug"
    text: str
    animation_tag: str | None = None
    emotion: str | None = None
    ttl_s: float = 600.0


@router.post("/v1/proactive/debug")
async def proactive_debug_endpoint(req: ProactiveDebugRequest) -> dict:
    """WP-25 accept test's door into the queue.

    only reachable with FELT_DEBUG=1, a 404 (not just an auth error) otherwise so its
    existence isn't even discoverable on a normal deployment.
    """
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
