"""WP-42.2: the Felt app's API, at felt.clockmost.com/v1/app/*.

The app is a browser page (and thin Tauri shells around it) served behind SSO,
so it can't hold felt-core's API token. Caddy runs forward-auth on exactly
these routes and passes Mario's identity as `X-Authentik-Username` (it drops
any client-sent copy first); api.py's `_SSO_GATED` lets them past the token
check, and every route here still refuses a request without that header, so
a route that somehow skipped Caddy fails closed.

It shares the "dashboard" session with the tray window, so the phone and the
desktop see one conversation.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from felt_core import margins
from felt_core.app_history import history_page
from felt_core.app_stream import notices
from felt_core.asides import current_asides, link_answer, remark_text
from felt_core.brain import chat
from felt_core.config import load_config
from felt_core.db import APP_SESSION
from felt_core.diary import he_is_here
from felt_core.marks import mark_seen, maybe_mark

SESSION = APP_SESSION

router = APIRouter(prefix="/v1/app")

# background mark decisions, kept referenced so they aren't garbage-collected
_pending: set[asyncio.Task] = set()


def schedule_mark(config, session_id: str = SESSION) -> None:
    """After a reply: maybe she marks his line (marks.py), off the hot path."""
    task = asyncio.create_task(maybe_mark(config, session_id))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def sso_user(x_authentik_username: str | None = Header(default=None)) -> str:
    if not x_authentik_username:
        raise HTTPException(status_code=401, detail="sign in through SSO")
    return x_authentik_username


class MarkOut(BaseModel):
    id: str
    type: str
    quote: str
    note: str = ""
    # drawn on some screen already: show it inked, don't animate
    seen: bool = False


class Remark(BaseModel):
    id: int
    content: str
    created_at: str


class Message(BaseModel):
    id: int
    role: str
    content: str
    created_at: str
    marks: list[MarkOut] = []
    # his line answering one of her asides: her remark, shown above it
    answering: Remark | None = None


class Asides(BaseModel):
    asides: list[Remark]
    more: int


class Me(BaseModel):
    user: str


class SayRequest(BaseModel):
    message: str
    # id of the aside he tapped to answer (GET /asides)
    answering: int | None = None


class SayResponse(BaseModel):
    # the stored user line and her reply, with ids, so the app can append
    # them without refetching the page
    messages: list[Message]


@router.get("/me", response_model=Me)
async def me(user: str = Depends(sso_user)) -> Me:
    return Me(user=user)


@router.get("/history", response_model=list[Message])
async def history(
    before: int | None = None, limit: int = 50, _user: str = Depends(sso_user)
) -> list[dict]:
    return history_page(load_config().db_path, SESSION, before_id=before, limit=limit)


@router.get("/asides", response_model=Asides)
async def asides(_user: str = Depends(sso_user)) -> dict:
    return current_asides(load_config().db_path, SESSION)


class MarginLine(BaseModel):
    id: int
    kind: str
    source: str
    content: str
    created_at: str
    read: bool


class Unread(BaseModel):
    unread: int


class MarkRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    action: Literal["read", "dismiss", "snooze"]
    hours: float | None = None


class MarkResult(BaseModel):
    changed: int


@router.get("/margins", response_model=list[MarginLine])
async def margins_page(
    before: int | None = None, limit: int = 50, _user: str = Depends(sso_user)
) -> list[dict]:
    return margins.page(load_config().db_path, SESSION, before_id=before, limit=limit)


@router.get("/margins/count", response_model=Unread)
async def margins_count(_user: str = Depends(sso_user)) -> Unread:
    return Unread(unread=margins.unread(load_config().db_path, SESSION))


@router.post("/margins/mark", response_model=MarkResult)
async def margins_mark(req: MarkRequest, _user: str = Depends(sso_user)) -> MarkResult:
    changed = margins.mark(
        load_config().db_path, req.ids, req.action, hours=req.hours, session_id=SESSION
    )
    return MarkResult(changed=changed)


class SeenRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)


@router.post("/marks/seen", response_model=MarkResult)
async def marks_seen(req: SeenRequest, _user: str = Depends(sso_user)) -> MarkResult:
    return MarkResult(changed=mark_seen(load_config().db_path, req.ids))


@router.get("/stream")
async def stream(request: Request, _user: str = Depends(sso_user)) -> StreamingResponse:
    """Live notices for the open app (app_stream.py). An app window opening
    is a sign of him, so it can start her day (diary.py)."""
    config = load_config()
    he_is_here(config)
    return StreamingResponse(
        notices(config.db_path, SESSION, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat", response_model=SayResponse)
async def say(req: SayRequest, _user: str = Depends(sso_user)) -> SayResponse:
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=422, detail="empty message")
    config = load_config()
    remark = None
    if req.answering is not None:
        remark = remark_text(config.db_path, req.answering, SESSION)
        if remark is None:
            raise HTTPException(status_code=422, detail="not one of her remarks")
    await chat(text, config, SESSION, answering=remark)
    rows = history_page(config.db_path, SESSION, limit=2)
    if req.answering is not None:
        mine = [r["id"] for r in rows if r["role"] == "user"]
        if mine:
            link_answer(config.db_path, mine[-1], req.answering)
            rows = history_page(config.db_path, SESSION, limit=2)
    schedule_mark(config)
    return SayResponse(messages=[Message(**r) for r in rows])
