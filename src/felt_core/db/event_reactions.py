"""The `event_reactions` table: her lines about desktop events."""

from sqlalchemy import func
from sqlmodel import col, select

from felt_core.db.session import session
from felt_core.db.tables import EventReaction


async def save_event_reaction(db_path: str, session_id: str, event: str, dedupe_key: str | None, line: str) -> None:
    """Store one spoken event line with its event type and dedupe key."""
    row = EventReaction(session_id=session_id, event=event, dedupe_key=dedupe_key, line=line)
    async with session(db_path) as s:
        s.add(row)
        await s.commit()


async def seconds_since_last_event_reaction(
    db_path: str, event: str | None = None, dedupe_key: str | None = None
) -> float | None:
    """Age of the most recent spoken event line.

    Any event (event=None), one event type, or one event+dedupe_key (e.g. the same track). None if there's never been
    one. Global across sessions on purpose: there's one Felt, and cooldowns are about how often SHE talks.
    """
    age = (func.julianday("now") - func.julianday(EventReaction.created_at)) * 86400.0
    query = select(age).order_by(col(EventReaction.id).desc()).limit(1)
    if event is not None:
        query = query.where(EventReaction.event == event)
    if dedupe_key is not None:
        query = query.where(EventReaction.dedupe_key == dedupe_key)
    async with session(db_path) as s:
        return (await s.exec(query)).first()


async def last_event_reaction_line(db_path: str, event: str) -> str | None:
    """The most recent spoken line for one event type (None if never).

    Lets an event avoid saying the exact same thing twice in a row.
    """
    query = select(EventReaction.line).where(EventReaction.event == event).order_by(col(EventReaction.id).desc())
    async with session(db_path) as s:
        return (await s.exec(query.limit(1))).first()
