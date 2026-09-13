"""The frontend API — what a dashboard, voice pipeline, or Android client
actually talks to. None of them touch brain internals directly.

v1, deliberately simple: one request in, one reply out, no session/
conversation-history handling yet (that's SQLite persistence, a separate
near-term addition — see assistant-core.md's Build phases; this phase is
just about having *an* API surface at all, versioned so it can evolve).
"""

import asyncio
import contextlib
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from felt_core.alarms import reschedule_pending_alarms
from felt_core.brain import chat
from felt_core.config import load_config
from felt_core.db import (
    add_shopping_item,
    clear_checked_shopping_items,
    delete_shopping_item,
    get_history,
    get_setting,
    get_shopping_items,
    set_setting,
    set_shopping_item_checked,
)
from felt_core.email_watcher import watch as email_watch
from felt_core.ntfy_listener import listen as ntfy_listen

_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    ntfy_task = asyncio.create_task(ntfy_listen(config))
    email_task = asyncio.create_task(email_watch(config))
    # A pending alarm set before a restart isn't just lost — see
    # alarms.py for why late-firing beats silently dropping it.
    reschedule_pending_alarms(config)
    yield
    ntfy_task.cancel()
    email_task.cancel()


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
    expose_headers=["X-Transcribed-Text", "X-Reply-Text"],
)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    reply: str


@app.post("/v1/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest) -> ChatResponse:
    config = load_config()
    reply = await chat(req.message, config, req.session_id)
    return ChatResponse(reply=reply)


@app.get("/v1/history")
async def history_endpoint(session_id: str = "default", limit: int = 100) -> list[dict]:
    """A client's chat window is otherwise blank on every restart even
    though the conversation is already durable in SQLite (`get_history`
    was previously only read internally, to build the brain's own
    context) — this just exposes that same data so a dashboard can
    restore what was actually said."""
    config = load_config()
    return get_history(config.db_path, session_id, limit)


@app.post("/v1/voice-chat")
async def voice_chat_endpoint(file: UploadFile = File(...), session_id: str = "default") -> Response:
    """One call: audio in, audio out. Orchestrates felt-voice (STT) ->
    the brain -> felt-voice (TTS), so a client doesn't have to."""
    config = load_config()
    if not config.voice_url:
        raise HTTPException(status_code=503, detail="Voice isn't configured (no FELT_VOICE_URL set).")

    async with httpx.AsyncClient(timeout=60) as client:
        stt_resp = await client.post(
            f"{config.voice_url}/v1/stt", files={"file": (file.filename, await file.read(), file.content_type)}
        )
        stt_resp.raise_for_status()
        transcribed = stt_resp.json()["text"]

        if transcribed.strip():
            reply = await chat(transcribed, config, session_id)
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

        tts_resp = await client.post(f"{config.voice_url}/v1/tts", json={"text": reply})
        tts_resp.raise_for_status()

    return Response(
        content=tts_resp.content,
        media_type="audio/wav",
        # Percent-encoded — headers must be ASCII-safe, a non-English reply
        # (accents, etc.) would otherwise break this.
        headers={"X-Transcribed-Text": quote(transcribed), "X-Reply-Text": quote(reply)},
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
            reply = await chat(data["message"], config, data.get("session_id", "default"))
            await websocket.send_json({"reply": reply})
    except Exception:
        await websocket.close()


def run() -> None:
    config = load_config()
    uvicorn.run(app, host=config.host, port=config.port)
