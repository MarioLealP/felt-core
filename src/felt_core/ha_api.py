"""Felt as Home Assistant's conversation agent (felt-ha-hub step 1, bead
cm-j7c): HA's "Felt" integration sends what Mario said to Assist - typed or
spoken, from the HA app on any device - and gets her reply back. Auth is in
ha_auth (checked by the middleware before anything else). Her own
conversation (session `ha`), same brain, skills and memory as everywhere.

Step 2 (2026-10-18): her own switches as HA switches - quiet, hide (the same
controls the app and the tray use, controls.py) and DJ mode - so the HA app
and automations can flip them without asking her.

The Systems summary (2026-10-18, Mario: "zabbix and wazuh and ntfy on it"):
the app's Systems page - Zabbix/Wazuh/ntfy alerts as triaged incidents -
per area plus the latest few, for the HA dashboard.

New mail (WP-43, 2026-10-27): the integration forwards HA's IMAP events
here; emails.py decides what she mentions.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from felt_core import controls, emails, local_llm, systems
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


class HaArea(BaseModel):
    status: str
    count: int


class HaIncident(BaseModel):
    id: int
    area: str
    status: str
    tier: str | None = None
    title: str
    last_at: str | None = None


class HaSystems(BaseModel):
    areas: dict[str, HaArea]
    needs_you: int
    latest: list[HaIncident]


@router.get("/systems", response_model=HaSystems)
async def ha_systems() -> HaSystems:
    page = await asyncio.to_thread(systems.page, load_config().db_path, None, 5)
    areas = {a["name"]: HaArea(status=a["status"], count=a["count"]) for a in page["areas"]}
    return HaSystems(
        areas=areas,
        needs_you=sum(a.count for a in areas.values() if a.status == "needs-you"),
        latest=[
            HaIncident(
                id=r["id"],
                area=r["area"],
                status=r["status"],
                tier=r.get("tier"),
                title=r["title"],
                last_at=r.get("last_at"),
            )
            for r in page["incidents"][:5]
        ],
    )


class HaEmail(BaseModel):
    # Message-ID, else the IMAP uid: the same mail forwarded twice is one mail
    key: str = Field(min_length=1, max_length=500)
    sender: str = Field(max_length=500)
    subject: str = Field(default="", max_length=1000)
    text: str = Field(default="", max_length=60_000)
    # mailing-list mail (a List-Unsubscribe header)
    bulk: bool = False


class HaEmailStored(BaseModel):
    new: bool


@router.post("/email", response_model=HaEmailStored)
async def ha_email(req: HaEmail) -> HaEmailStored:
    mail_id = await asyncio.to_thread(
        emails.add, load_config().db_path, req.key, req.sender, req.subject, req.text, req.bulk
    )
    return HaEmailStored(new=mail_id is not None)
