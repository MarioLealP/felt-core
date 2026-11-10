"""The `proactive` table: the queue of things she brings up on her own."""

from felt_core.db.session import _connect, _require_lastrowid


def insert_proactive(db_path: str, kind: str, text: str, payload_json: str, expires_at: str | None) -> int:
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute(
            "INSERT INTO proactive (kind, text, payload_json, expires_at) VALUES (?, ?, ?, ?)",
            (kind, text, payload_json, expires_at),
        )
        item_id = _require_lastrowid(cursor)
    conn.close()
    return item_id


def _row_to_proactive_dict(row: tuple) -> dict:
    return {
        "id": row[0],
        "kind": row[1],
        "text": row[2],
        "payload_json": row[3],
        "created_at": row[4],
        "expires_at": row[5],
    }


def claim_next_proactive(db_path: str) -> dict | None:
    """Atomically returns and marks-delivered the oldest undelivered,
    unexpired proactive item (WP-25's long-poll endpoint calls this on
    every poll tick), or None if there's nothing to hand out right now.
    Select-then-update in one `with conn:` transaction so two concurrent
    long-poll callers can't both walk away with the same row. An expired
    item is excluded here, not just filtered by a caller - the accept
    test ("an expired one never is [spoken]") depends on this query never
    returning one, not on a caller remembering to check.
    """
    conn = _connect(db_path)
    row = None
    with conn:
        row = conn.execute(
            "SELECT id, kind, text, payload_json, created_at, expires_at FROM proactive "
            "WHERE delivered_at IS NULL AND (expires_at IS NULL OR expires_at > datetime('now')) "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is not None:
            conn.execute("UPDATE proactive SET delivered_at = datetime('now') WHERE id = ?", (row[0],))
    conn.close()
    return _row_to_proactive_dict(row) if row is not None else None


def get_proactive_item(db_path: str, item_id: int) -> dict | None:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT id, kind, text, payload_json, created_at, expires_at FROM proactive WHERE id = ?",
        (item_id,),
    ).fetchone()
    conn.close()
    return _row_to_proactive_dict(row) if row else None


def requeue_proactive(db_path: str, item_id: int) -> bool:
    """Clears `delivered_at` so the item is handed out again on the next
    poll - felt-tray's path for "Felt was hidden/away and this wasn't
    urgent, don't lose it." Only takes effect if the item still exists and
    hasn't since expired (an expired item must never come back around,
    same rule claim_next_proactive enforces) - returns whether it did.
    """
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute(
            "UPDATE proactive SET delivered_at = NULL WHERE id = ? "
            "AND (expires_at IS NULL OR expires_at > datetime('now'))",
            (item_id,),
        )
        requeued = cursor.rowcount > 0
    conn.close()
    return requeued


def prune_expired_proactive(db_path: str) -> None:
    """Startup sweep (same convention as delete_old_latency_rows) - an
    expired item is already unreachable via claim_next_proactive, this
    just stops the table growing forever.
    """
    conn = _connect(db_path)
    with conn:
        conn.execute("DELETE FROM proactive WHERE expires_at IS NOT NULL AND expires_at <= datetime('now')")
    conn.close()
