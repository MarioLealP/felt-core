"""Chat: POST /v1/chat, the reaction tag, history and the /v1/chat/ws websocket."""

import json
import logging
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel

from felt_core.api.auth import _ws_authorized
from felt_core.brain import (
    chat,
)
from felt_core.clients import local_llm
from felt_core.config import load_config
from felt_core.core import metrics
from felt_core.db.messages import get_history

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    """Request body for a chat turn: message, session and optional tone."""

    message: str
    session_id: str = "default"
    # WP-15: felt-animation's GET /tone line (mood x time-of-day + streak
    # awareness), fetched and attached by felt-tray/felt-dashboard. Optional
    # so an older client (or one with no felt-animation running) is unaffected.
    tone: str | None = None


class ChatResponse(BaseModel):
    """Felt's reply text plus optional ui_action and animation_tag."""

    reply: str
    # Both purely additive - an older client that only reads `reply` is
    # unaffected. ui_action mirrors SkillResult.ui_action (e.g.
    # {"kind": "toast", "payload": {...}}); animation_tag is what
    # felt-tray should relay to felt-overlay (see felt_core.core.animation).
    # Both are None for a plain conversational reply with no triggered
    # skill, or a skill whose tag/ui_action just haven't been set yet.
    ui_action: dict[str, Any] | None = None
    animation_tag: str | None = None


@router.post("/v1/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest) -> ChatResponse:
    """Run one chat turn through the brain and return Felt's reply."""
    config = load_config()
    result = await chat(req.message, config, req.session_id, req.tone)
    return ChatResponse(
        reply=result.reply,
        ui_action=result.ui_action.model_dump() if result.ui_action else None,
        animation_tag=result.animation_tag,
    )


class ReactionTagRequest(BaseModel):
    """A spoken line plus the animation tags to choose from."""

    text: str
    tags: list[str]


@router.post("/v1/reaction-tag")
async def reaction_tag_endpoint(req: ReactionTagRequest) -> dict:
    """WP-36 D'': pick the animation tag that best fits how Felt says a line.

    felt-animation sends its known tags (it owns the animation
    library); ONE schema-constrained felt-llm call (the tag is an enum of
    exactly those). `{"tag": null}` if the reply couldn't be parsed; 400
    for an empty tag list; 503 when felt-llm is down (the caller keeps its
    fallback tag).
    """
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
    except json.JSONDecodeError, AttributeError:
        return {"tag": None}
    return {"tag": tag if isinstance(tag, str) and tag in req.tags else None}


@router.get("/v1/history")
async def history_endpoint(session_id: str = "default", limit: int = 100) -> list[dict]:
    """A client's chat window is otherwise blank on every restart.

    Even though the conversation is already durable in SQLite (`get_history`
    was previously only read internally, to build the brain's own
    context) — this just exposes that same data so a dashboard can
    restore what was actually said.
    """
    config = load_config()
    return await get_history(config.db_path, session_id, limit)


@router.websocket("/v1/chat/ws")
async def chat_ws(websocket: WebSocket) -> None:
    """Chat over the /v1/chat/ws websocket until the client disconnects."""
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
            except local_llm.LocalLLMUnavailableError:
                # Same contract as the HTTP 503: the socket stays open.
                await websocket.send_json({"error": "felt-llm offline"})
                continue
            await websocket.send_json(
                {
                    "reply": result.reply,
                    "ui_action": result.ui_action.model_dump() if result.ui_action else None,
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
