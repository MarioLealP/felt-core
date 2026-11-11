"""The `self_memory` table: what she takes away from a day (WP-26 / personality plan P4)."""

from typing import Any

from felt_core.db.session import _connect, _require_lastrowid


def insert_self_memory(db_path: str, kind: str, text: str, keywords_json: str, source_date: str | None) -> int:
    """Store one self-memory row and return its id."""
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute(
            "INSERT INTO self_memory (kind, text, keywords_json, source_date) VALUES (?, ?, ?, ?)",
            (kind, text, keywords_json, source_date),
        )
        memory_id = _require_lastrowid(cursor)
    conn.close()
    return memory_id


_SELF_MEMORY_COLUMNS = "id, kind, text, keywords_json, source_date, uses, last_used, status, pinned, edited, created_at"


def _row_to_self_memory(row: tuple) -> dict:
    return {
        "id": row[0],
        "kind": row[1],
        "text": row[2],
        "keywords_json": row[3],
        "source_date": row[4],
        "uses": row[5],
        "last_used": row[6],
        "status": row[7],
        "pinned": bool(row[8]),
        "edited": bool(row[9]),
        "created_at": row[10],
    }


def get_self_memory(db_path: str, memory_id: int) -> dict | None:
    """Return one self-memory row by id, or None."""
    conn = _connect(db_path)
    row = conn.execute(f"SELECT {_SELF_MEMORY_COLUMNS} FROM self_memory WHERE id = ?", (memory_id,)).fetchone()  # ruff: ignore[hardcoded-sql-expression]  # constant SQL: only ? placeholders and fixed columns go in
    conn.close()
    return _row_to_self_memory(row) if row else None


def get_self_memories(db_path: str, status: str | None = None) -> list[dict]:
    """Return self-memories newest first, optionally only those with a status."""
    conn = _connect(db_path)
    if status is not None:
        rows = conn.execute(
            f"SELECT {_SELF_MEMORY_COLUMNS} FROM self_memory WHERE status = ? ORDER BY id DESC",  # ruff: ignore[hardcoded-sql-expression]  # constant SQL: only ? placeholders and fixed columns go in
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(f"SELECT {_SELF_MEMORY_COLUMNS} FROM self_memory ORDER BY id DESC").fetchall()  # ruff: ignore[hardcoded-sql-expression]  # constant SQL: only ? placeholders and fixed columns go in
    conn.close()
    return [_row_to_self_memory(r) for r in rows]


def get_vetoed_self_memory_texts(db_path: str) -> list[str]:
    """Return the texts Mario vetoed, for the never-propose list."""
    conn = _connect(db_path)
    rows = conn.execute("SELECT text FROM self_memory WHERE status = 'vetoed'").fetchall()
    conn.close()
    return [r[0] for r in rows]


def count_active_self_memories(db_path: str) -> int:
    """Count the self-memories with status active."""
    conn = _connect(db_path)
    row = conn.execute("SELECT COUNT(*) FROM self_memory WHERE status = 'active'").fetchone()
    conn.close()
    return row[0]


def update_self_memory(
    db_path: str,
    memory_id: int,
    *,
    text: str | None = None,
    pinned: bool | None = None,
    status: str | None = None,
) -> bool:
    """Mario's edit/pin/veto/retire actions (PATCH /v1/self-memory/{id}).

    Only the fields actually passed are touched; a text edit also marks
    the row `edited` (same convention as diary's PATCH).
    """
    sets: list[str] = []
    params: list[Any] = []
    if text is not None:
        sets.append("text = ?")
        params.append(text)
        sets.append("edited = 1")
    if pinned is not None:
        sets.append("pinned = ?")
        params.append(int(pinned))
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if not sets:
        return False
    params.append(memory_id)
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute(f"UPDATE self_memory SET {', '.join(sets)} WHERE id = ?", params)  # ruff: ignore[hardcoded-sql-expression]  # constant SQL: only ? placeholders and fixed columns go in
        updated = cursor.rowcount > 0
    conn.close()
    return updated


def delete_self_memory(db_path: str, memory_id: int) -> bool:
    """Delete a self-memory row and return whether it existed."""
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute("DELETE FROM self_memory WHERE id = ?", (memory_id,))
        deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def touch_self_memories(db_path: str, memory_ids: list[int]) -> None:
    """Increments `uses` and stamps `last_used` for every memory actually placed in a prompt.

    (self_memory.memory_block) - what the ranking's recency/cooldown signals
    are computed from next time.
    """
    if not memory_ids:
        return
    conn = _connect(db_path)
    with conn:
        conn.executemany(
            "UPDATE self_memory SET uses = uses + 1, last_used = datetime('now') WHERE id = ?",
            [(mid,) for mid in memory_ids],
        )
    conn.close()


def retire_self_memories(db_path: str, memory_ids: list[int]) -> None:
    """Retire the given self-memories while they're still active."""
    if not memory_ids:
        return
    conn = _connect(db_path)
    with conn:
        conn.executemany(
            "UPDATE self_memory SET status = 'retired' WHERE id = ? AND status = 'active'",
            [(mid,) for mid in memory_ids],
        )
    conn.close()
