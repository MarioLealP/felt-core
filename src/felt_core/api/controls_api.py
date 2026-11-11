"""WP-42.5: the controls' routes - the app's (SSO) and the desktop tray's (bearer token).

The switches themselves live in controls.py.
"""

import asyncio
from typing import Annotated, Literal

import httpx2
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from felt_core.api.app_api import sso_user
from felt_core.clients import local_llm
from felt_core.clients.felt_tls import voice_client
from felt_core.config import FeltConfig, load_config
from felt_core.core import controls, features
from felt_core.core.plugin_loader import load_plugins

router = APIRouter()


class SetControl(BaseModel):
    """A control change: hide or quiet, on or off, for some minutes."""

    name: Literal["hide", "quiet"]
    on: bool
    minutes: int | None = None


async def _voice_up(config: FeltConfig) -> bool | None:
    if not config.voice_url:
        return None
    try:
        async with voice_client(config, timeout=2.0) as client:
            await client.get(config.voice_url)
        return True  # any answer means it's there
    except httpx2.HTTPError:
        return False


async def _page(config: FeltConfig) -> dict:
    db = config.db_path
    now = await controls.state(db)
    desk = await controls.desktop(db)
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
async def app_controls(_user: Annotated[str, Depends(sso_user)]) -> dict:
    """The controls page data: the switches, the desktop state and the parts' health."""
    return await _page(load_config())


@router.post("/v1/app/controls")
async def app_set_control(req: SetControl, _user: Annotated[str, Depends(sso_user)]) -> dict:
    """Set one control (hide or quiet), then return the controls page data."""
    if req.minutes is not None and req.minutes not in controls.TIMERS:
        raise HTTPException(status_code=422, detail=f"minutes must be one of {controls.TIMERS}")
    config = load_config()
    await controls.set_control(config.db_path, req.name, req.on, req.minutes)
    return await _page(config)


class Settings(BaseModel):
    """The app settings body: the Claude-alerts switch."""

    claude_alerts: bool


@router.get("/v1/app/settings")
async def app_settings(_user: Annotated[str, Depends(sso_user)]) -> dict:
    """Return the app settings: whether the server's Claude looks into alerts."""
    db = load_config().db_path
    return {"claude_alerts": await controls.claude_alerts(db)}


@router.post("/v1/app/settings")
async def app_set_settings(req: Settings, _user: Annotated[str, Depends(sso_user)]) -> dict:
    """WP-58's kill switch: whether the server's Claude looks into alerts."""
    db = load_config().db_path
    await controls.set_claude_alerts(db, req.claude_alerts)
    return {"claude_alerts": await controls.claude_alerts(db)}


class SetFeature(BaseModel):
    """A feature switch change: which feature key, on or off."""

    key: str
    on: bool


async def _features(config: FeltConfig) -> list[dict]:
    return await features.listing(config.db_path, list(load_plugins(config.plugins_dir)))


@router.get("/v1/app/features")
async def app_features(_user: Annotated[str, Depends(sso_user)]) -> list[dict]:
    """Her feature switches (features.py), each with whether it's on."""
    return await _features(load_config())


@router.post("/v1/app/features")
async def app_set_feature(req: SetFeature, _user: Annotated[str, Depends(sso_user)]) -> list[dict]:
    """Turn one feature or skill plugin on or off, then return the listing."""
    config = load_config()
    is_skill = req.key.startswith(features.SKILL_PREFIX) and (
        req.key.removeprefix(features.SKILL_PREFIX) in load_plugins(config.plugins_dir)
    )
    if req.key not in features.FEATURES and not is_skill:
        raise HTTPException(status_code=422, detail=f"unknown feature {req.key!r}")
    await features.set_enabled(config.db_path, req.key, req.on)
    return await _features(config)


@router.get("/v1/controls")
async def desktop_controls() -> dict:
    """The tray: what the switches say, to apply to felt-animation."""
    return await controls.state(load_config().db_path)


@router.post("/v1/controls/desktop")
async def desktop_report(report: dict) -> dict:
    """The tray's heartbeat: her real state on the PC."""
    await controls.report_desktop(load_config().db_path, report)
    return {"ok": True}
