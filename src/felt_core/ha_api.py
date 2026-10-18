"""Felt as Home Assistant's conversation agent (felt-ha-hub step 1, bead
cm-j7c): HA's "Felt" integration sends what Mario said to Assist - typed or
spoken, from the HA app on any device - and gets her reply back. Auth is in
ha_auth (checked by the middleware before anything else). Her own
conversation (session `ha`), same brain, skills and memory as everywhere.

Step 2 (2026-10-18): her own switches as HA switches - quiet, hide (the same
controls the app and the tray use, controls.py) and DJ mode - so the HA app
and automations can flip them without asking her.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from felt_core import controls, local_llm
from felt_core.brain import chat
from felt_core.config import FeltConfig, load_config
from felt_core.dj import control as dj_control

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


class HaControls(BaseModel):
    quiet: bool
    hide: bool
    dj: bool
    desktop_online: bool
    mood: str | None = None
    message: str | None = None


class HaSetControl(BaseModel):
    name: Literal["quiet", "hide", "dj"]
    on: bool


async def _controls(config: FeltConfig, message: str | None = None) -> HaControls:
    now = await asyncio.to_thread(controls.state, config.db_path)
    desk = await asyncio.to_thread(controls.desktop, config.db_path)
    return HaControls(
        quiet=now["quiet"]["on"],
        hide=now["hide"]["on"],
        dj=dj_control.is_on(),
        desktop_online=desk is not None,
        mood=desk.get("mood") if desk else None,
        message=message,
    )


@router.get("/controls", response_model=HaControls)
async def ha_controls() -> HaControls:
    return await _controls(load_config())


@router.post("/controls", response_model=HaControls)
async def ha_set_control(req: HaSetControl) -> HaControls:
    config = load_config()
    message = None
    if req.name == "dj":
        if req.on and not dj_control.is_on():
            message = await dj_control.start(config)  # says why when it can't (Spotify asleep)
        elif not req.on:
            dj_control.stop("turned off in Home Assistant")
    else:
        await asyncio.to_thread(controls.set_control, config.db_path, req.name, req.on)
    return await _controls(config, message)
