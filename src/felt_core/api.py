"""The frontend API — what a dashboard, voice pipeline, or Android client
actually talks to. None of them touch brain internals directly.

v1, deliberately simple: one request in, one reply out, no session/
conversation-history handling yet (that's SQLite persistence, a separate
near-term addition — see assistant-core.md's Build phases; this phase is
just about having *an* API surface at all, versioned so it can evolve).
"""

import asyncio
import contextlib
from urllib.parse import quote

import httpx
import uvicorn
from typing import Literal

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from felt_core.alarms import reschedule_pending_alarms
from felt_core.brain import chat
from felt_core.config import load_config
from felt_core.db import get_setting, set_setting
from felt_core.ntfy_listener import listen as ntfy_listen


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    task = asyncio.create_task(ntfy_listen(config))
    # A pending alarm set before a restart isn't just lost — see
    # alarms.py for why late-firing beats silently dropping it.
    reschedule_pending_alarms(config)
    yield
    task.cancel()


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

        reply = await chat(transcribed, config, session_id)

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
