"""WP-42.5: the controls' routes - the app's (SSO) and the desktop tray's
(bearer token). The switches themselves live in controls.py."""

import asyncio
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from felt_core import controls, local_llm
from felt_core.app_api import sso_user
from felt_core.config import FeltConfig, load_config

router = APIRouter()


class SetControl(BaseModel):
    name: Literal["hide", "quiet"]
    on: bool
    minutes: int | None = None


async def _voice_up(config: FeltConfig) -> bool | None:
    if not config.voice_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.get(config.voice_url)
        return True  # any answer means it's there
    except httpx.HTTPError:
        return False


async def _page(config: FeltConfig) -> dict:
    db = config.db_path
    now = await asyncio.to_thread(controls.state, db)
    desk = await asyncio.to_thread(controls.desktop, db)
    model, voice = await asyncio.gather(local_llm.is_healthy(config), _voice_up(config))
    return {
        **now,
        "timers": list(controls.TIMERS),
        "desktop": desk,
        "parts": {
            "brain": True,
            "model": model,
            "voice": voice,
            "desktop": desk is not None,
            "animation": bool(desk and desk.get("animation")),
        },
        "mood": desk.get("mood") if desk else None,
    }


@router.get("/v1/app/controls")
async def app_controls(_user: str = Depends(sso_user)) -> dict:
    return await _page(load_config())


@router.post("/v1/app/controls")
async def app_set_control(req: SetControl, _user: str = Depends(sso_user)) -> dict:
    if req.minutes is not None and req.minutes not in controls.TIMERS:
        raise HTTPException(status_code=422, detail=f"minutes must be one of {controls.TIMERS}")
    config = load_config()
    await asyncio.to_thread(controls.set_control, config.db_path, req.name, req.on, req.minutes)
    return await _page(config)


class Settings(BaseModel):
    claude_alerts: bool


@router.get("/v1/app/settings")
async def app_settings(_user: str = Depends(sso_user)) -> dict:
    db = load_config().db_path
    return {"claude_alerts": await asyncio.to_thread(controls.claude_alerts, db)}


@router.post("/v1/app/settings")
async def app_set_settings(req: Settings, _user: str = Depends(sso_user)) -> dict:
    """WP-58's kill switch: whether the server's Claude looks into alerts."""
    db = load_config().db_path
    await asyncio.to_thread(controls.set_claude_alerts, db, req.claude_alerts)
    return {"claude_alerts": await asyncio.to_thread(controls.claude_alerts, db)}


@router.get("/v1/controls")
async def desktop_controls() -> dict:
    """The tray: what the switches say, to apply to felt-animation."""
    return await asyncio.to_thread(controls.state, load_config().db_path)


@router.post("/v1/controls/desktop")
async def desktop_report(report: dict) -> dict:
    """The tray's heartbeat: her real state on the PC."""
    await asyncio.to_thread(controls.report_desktop, load_config().db_path, report)
    return {"ok": True}
