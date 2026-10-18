"""Felt as Home Assistant's conversation agent (felt-ha-hub step 1, bead
cm-j7c): HA's "Felt" integration sends what Mario said to Assist - typed or
spoken, from the HA app on any device - and gets her reply back. Auth is in
ha_auth (checked by the middleware before anything else). Her own
conversation (session `ha`), same brain, skills and memory as everywhere.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from felt_core import local_llm
from felt_core.brain import chat
from felt_core.config import load_config

SESSION = "ha"

router = APIRouter(prefix="/v1/ha")


class HaChat(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    language: str | None = None


class HaReply(BaseModel):
    reply: str


@router.post("/chat", response_model=HaReply)
async def ha_chat(req: HaChat) -> HaReply:
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="empty message")
    try:
        result = await chat(text, load_config(), SESSION)
    except local_llm.LocalLLMUnavailable as e:
        raise HTTPException(status_code=503, detail="felt-llm unavailable") from e
    return HaReply(reply=result.reply)
