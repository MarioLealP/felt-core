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

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from felt_core.app_history import history_page
from felt_core.brain import chat
from felt_core.config import load_config
from felt_core.marks import maybe_mark

SESSION = "dashboard"

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


class Message(BaseModel):
    id: int
    role: str
    content: str
    created_at: str
    marks: list[MarkOut] = []


class Me(BaseModel):
    user: str


class SayRequest(BaseModel):
    message: str


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


@router.post("/chat", response_model=SayResponse)
async def say(req: SayRequest, _user: str = Depends(sso_user)) -> SayResponse:
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=422, detail="empty message")
    config = load_config()
    await chat(text, config, SESSION)
    schedule_mark(config)
    rows = history_page(config.db_path, SESSION, limit=2)
    return SayResponse(messages=[Message(**r) for r in rows])
