"""The `alarms` table."""

from sqlalchemy import delete, update
from sqlmodel import col, select

from felt_core.db.session import require_id, session
from felt_core.db.tables import Alarm


async def save_alarm(db_path: str, fire_at_iso: str, label: str) -> int:
    """Store one alarm row and return its id."""
    row = Alarm(fire_at=fire_at_iso, label=label)
    async with session(db_path) as s:
        s.add(row)
        await s.commit()
    return require_id(row)


async def get_pending_alarms(db_path: str) -> list[dict]:
    """Return every unfired alarm, ordered by fire time."""
    async with session(db_path) as s:
        rows = await s.exec(select(Alarm).where(Alarm.fired == 0).order_by(col(Alarm.fire_at)))
        return [{"id": r.id, "fire_at": r.fire_at, "label": r.label} for r in rows]


async def mark_alarm_fired(db_path: str, alarm_id: int) -> None:
    """Mark one alarm as fired."""
    async with session(db_path) as s:
        await s.exec(update(Alarm).where(col(Alarm.id) == alarm_id).values(fired=1))
        await s.commit()


async def delete_alarm(db_path: str, alarm_id: int) -> None:
    """Distinct from mark_alarm_fired: a cancelled alarm never fired.

    Marking it 'fired' would be a lie — this removes the row outright.
    """
    async with session(db_path) as s:
        await s.exec(delete(Alarm).where(col(Alarm.id) == alarm_id))
        await s.commit()
