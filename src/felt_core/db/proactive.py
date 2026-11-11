"""The `proactive` table: the queue of things she brings up on her own."""

from sqlalchemy import delete, func, or_, update
from sqlmodel import col, select

from felt_core.db.session import require_id, session
from felt_core.db.tables import Proactive


def _unexpired():
    return or_(col(Proactive.expires_at).is_(None), col(Proactive.expires_at) > func.datetime("now"))


def _as_dict(row: Proactive) -> dict:
    return row.model_dump(include={"id", "kind", "text", "payload_json", "created_at", "expires_at"})


async def insert_proactive(db_path: str, kind: str, text: str, payload_json: str, expires_at: str | None) -> int:
    """Queue one proactive item; returns its id."""
    row = Proactive(kind=kind, text=text, payload_json=payload_json, expires_at=expires_at)
    async with session(db_path) as s:
        s.add(row)
        await s.commit()
    return require_id(row)


async def claim_next_proactive(db_path: str) -> dict | None:
    """Hands out the oldest undelivered, unexpired proactive item, or None.

    One statement (UPDATE ... RETURNING): picks the oldest undelivered,
    unexpired item and marks it delivered atomically, so two concurrent
    long-poll callers (WP-25's endpoint calls this on every poll tick) can't
    both walk away with the same row. An expired item is excluded here, not
    just filtered by a caller - the accept test ("an expired one never is
    [spoken]") depends on this query never returning one.
    """
    oldest = (
        select(Proactive.id)
        .where(col(Proactive.delivered_at).is_(None), _unexpired())
        .order_by(col(Proactive.id))
        .limit(1)
        .scalar_subquery()
    )
    claim = (
        update(Proactive)
        .where(col(Proactive.id) == oldest)
        .values(delivered_at=func.datetime("now"))
        .returning(col(Proactive.id))
    )
    async with session(db_path) as s:
        claimed = (await s.exec(claim)).first()
        row = await s.get(Proactive, claimed[0]) if claimed is not None else None
        await s.commit()
    return _as_dict(row) if row is not None else None


async def get_proactive_item(db_path: str, item_id: int) -> dict | None:
    """One proactive item by id, or None."""
    async with session(db_path) as s:
        row = await s.get(Proactive, item_id)
    return _as_dict(row) if row is not None else None


async def requeue_proactive(db_path: str, item_id: int) -> bool:
    """Puts a delivered item back in the queue, unless it's gone or expired.

    Clears `delivered_at` so the item is handed out again on the next
    poll - felt-tray's path for "Felt was hidden/away and this wasn't
    urgent, don't lose it." Only takes effect if the item still exists and
    hasn't since expired (an expired item must never come back around,
    same rule claim_next_proactive enforces) - returns whether it did.
    """
    requeue = (
        update(Proactive)
        .where(col(Proactive.id) == item_id, _unexpired())
        .values(delivered_at=None)
        .returning(col(Proactive.id))
    )
    async with session(db_path) as s:
        requeued = (await s.exec(requeue)).first() is not None
        await s.commit()
    return requeued


async def prune_expired_proactive(db_path: str) -> None:
    """Startup sweep: deletes expired proactive items.

    Startup sweep (same convention as delete_old_latency_rows) - an
    expired item is already unreachable via claim_next_proactive, this
    just stops the table growing forever.
    """
    expired = col(Proactive.expires_at).is_not(None) & (col(Proactive.expires_at) <= func.datetime("now"))
    async with session(db_path) as s:
        await s.exec(delete(Proactive).where(expired))
        await s.commit()
