"""The `ntfy_events` table: what came in over ntfy."""

from sqlmodel import col, select

from felt_core.db.session import require_id, session
from felt_core.db.tables import NtfyEvent


async def save_ntfy_event(db_path: str, title: str | None, message: str, priority: int | None) -> int:
    """Store one ntfy event and return its id."""
    row = NtfyEvent(title=title, message=message, priority=priority)
    async with session(db_path) as s:
        s.add(row)
        await s.commit()
    return require_id(row)


async def get_recent_ntfy_events(db_path: str, limit: int = 10) -> list[dict]:
    """Return the most recent ntfy events, newest first."""
    async with session(db_path) as s:
        rows = await s.exec(select(NtfyEvent).order_by(col(NtfyEvent.id).desc()).limit(limit))
        return [
            {"title": r.title, "message": r.message, "priority": r.priority, "received_at": r.received_at} for r in rows
        ]
