"""The frontend API — what a dashboard, voice pipeline, or Android client
actually talks to. None of them touch brain internals directly.

v1, deliberately simple: one request in, one reply out, no session/
conversation-history handling yet (that's SQLite persistence, a separate
near-term addition — see assistant-core.md's Build phases; this phase is
just about having *an* API surface at all, versioned so it can evolve).
"""

import asyncio
import contextlib

import uvicorn
from fastapi import FastAPI, WebSocket
from pydantic import BaseModel

from felt_core.brain import chat
from felt_core.config import load_config
from felt_core.ntfy_listener import listen as ntfy_listen


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    task = asyncio.create_task(ntfy_listen(config))
    yield
    task.cancel()


app = FastAPI(title="felt-core", version="0.1.0", lifespan=lifespan)


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
