"""The `messages` table: chat turns and every line she says (kinds in db/kinds.py)."""

from sqlalchemy import func
from sqlmodel import col, select

from felt_core.db.kinds import ASIDE_KINDS, CHAT
from felt_core.db.session import require_id, session
from felt_core.db.tables import Message

# Legacy "[System Observation" rows (2026-09-22, WP-02): screen observations used to be saved into this table as fake
# `user` messages, before screen_observations got its own table. Hidden from the brain's context and /v1/history
# without deleting anything.
_NOT_OBSERVATION = col(Message.content).not_like("[System Observation%")


async def save_message(
    db_path: str,
    session_id: str,
    role: str,
    content: str,
    kind: str = CHAT,
    source_ref: str | None = None,
) -> int:
    """Store one message row and return its id."""
    row = Message(session_id=session_id, role=role, content=content, kind=kind, source_ref=source_ref)
    async with session(db_path) as s:
        s.add(row)
        await s.commit()
    return require_id(row)


async def get_history(db_path: str, session_id: str, limit: int = 20, exclude_remarks: bool = False) -> list[dict]:
    """`exclude_remarks=True` drops her own unprompted lines (kind other than chat).

    Leaving only the real back-and-forth - what the local screen-reaction
    path wants as "recent conversation" (audit 2026-09-24: it was feeding
    her earlier screen remarks back to her).
    """
    # newest first so LIMIT keeps the MOST RECENT messages (ASC would keep returning the oldest ones once a session
    # passes the limit - 2026-09-14 review finding), reversed back to chronological order
    query = select(Message.role, Message.content).where(Message.session_id == session_id, _NOT_OBSERVATION)
    if exclude_remarks:
        query = query.where(Message.kind == CHAT)
    async with session(db_path) as s:
        rows = list(await s.exec(query.order_by(col(Message.id).desc()).limit(limit)))
    return [{"role": role, "content": content} for role, content in reversed(rows)]


async def chat_page(db_path: str, session_id: str, before_id: int | None, limit: int) -> list[tuple]:
    """The conversation's newest `limit` rows older than `before_id`: (id, role, content, created_at), newest first."""
    query = select(Message.id, Message.role, Message.content, Message.created_at).where(
        Message.session_id == session_id, _NOT_OBSERVATION, Message.kind == CHAT
    )
    if before_id is not None:
        query = query.where(col(Message.id) < before_id)
    async with session(db_path) as s:
        return list(await s.exec(query.order_by(col(Message.id).desc()).limit(limit)))


async def get_her_recent_lines(db_path: str, session_id: str, limit: int = 8, max_age: str = "-6 hours") -> list[dict]:
    """Her own unprompted lines: the newest `limit` within `max_age`, oldest first.

    Kinds: remarks, event lines, pushes, mail; not alarms, which are his own
    labels. Fed back to her so she knows what she already said outside the
    conversation and stops repeating it (Mario 10-03).
    """
    query = (
        select(Message.content, Message.created_at)
        .where(
            Message.session_id == session_id,
            Message.role == "assistant",
            col(Message.kind).in_(ASIDE_KINDS),
            col(Message.created_at) >= func.datetime("now", max_age),
        )
        .order_by(col(Message.id).desc())
        .limit(limit)
    )
    async with session(db_path) as s:
        rows = list(await s.exec(query))
    return [{"content": c, "created_at": t} for c, t in reversed(rows)]


async def get_message_sessions_between(db_path: str, start_utc: str, end_utc: str) -> list[list[dict]]:
    """Real conversation (not her own unprompted remarks) in the window.

    Grouped by session - the diary's material gatherer treats each
    session's text as a WHOLE window, so sessions must come back
    separately, not pre-flattened.
    """
    query = (
        select(Message.session_id, Message.role, Message.content)
        .where(
            col(Message.created_at) >= start_utc,
            col(Message.created_at) < end_utc,
            _NOT_OBSERVATION,
            Message.kind == CHAT,
        )
        .order_by(col(Message.id))
    )
    async with session(db_path) as s:
        rows = await s.exec(query)
        sessions: dict[str, list[dict]] = {}
        for session_id, role, content in rows:
            sessions.setdefault(session_id, []).append({"role": role, "content": content})
    return list(sessions.values())
