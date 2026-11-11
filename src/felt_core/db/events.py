"""The `events` table: the silent desktop-event log (WP-26, POST /v1/events)."""

from sqlmodel import col, select

from felt_core.db.session import require_id, session
from felt_core.db.tables import Event


async def save_event(db_path: str, kind: str, data_json: str, at_utc: str) -> int:
    """Log one desktop event; returns its id."""
    row = Event(kind=kind, data_json=data_json, at=at_utc)
    async with session(db_path) as s:
        s.add(row)
        await s.commit()
    return require_id(row)


async def get_events_between(db_path: str, start_utc: str, end_utc: str) -> list[dict]:
    """Every logged event in [start_utc, end_utc).

    Every logged event whose `at` falls in [start_utc, end_utc) - both
    already-formatted 'YYYY-MM-DD HH:MM:SS' UTC strings, same convention as
    every other timestamp column here, so plain string comparison works.
    """
    query = select(Event).where(Event.at >= start_utc, Event.at < end_utc).order_by(col(Event.id))
    async with session(db_path) as s:
        return [{"kind": r.kind, "data_json": r.data_json, "at": r.at} for r in await s.exec(query)]
