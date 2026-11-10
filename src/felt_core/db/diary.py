"""The `diary` and `diary_weeks` tables (WP-26: the diary)."""

from felt_core.db.session import _connect


def get_diary(db_path: str, date: str) -> dict | None:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT date, entry, callbacks_json, mood_word, edited, created_at FROM diary WHERE date = ?",
        (date,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "date": row[0],
        "entry": row[1],
        "callbacks_json": row[2],
        "mood_word": row[3],
        "edited": bool(row[4]),
        "created_at": row[5],
    }


def insert_diary(db_path: str, date: str, entry: str, callbacks_json: str, mood_word: str) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO diary (date, entry, callbacks_json, mood_word) VALUES (?, ?, ?, ?)",
            (date, entry, callbacks_json, mood_word),
        )
    conn.close()


def update_diary_entry(db_path: str, date: str, entry: str, callbacks_json: str) -> bool:
    """Mario's edit (PATCH /v1/diary/{date}) - marks the row edited and
    replaces its callbacks (regenerated or cleared by the caller, see
    api.py). Returns whether a row existed to update.
    """
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute(
            "UPDATE diary SET entry = ?, callbacks_json = ?, edited = 1 WHERE date = ?",
            (entry, callbacks_json, date),
        )
        updated = cursor.rowcount > 0
    conn.close()
    return updated


def update_diary_callbacks(db_path: str, date: str, callbacks_json: str) -> None:
    """Internal decay bookkeeping (self_memory.memory_block increments a
    used callback's count) - deliberately doesn't touch `edited`, since
    this isn't Mario correcting anything.
    """
    conn = _connect(db_path)
    with conn:
        conn.execute("UPDATE diary SET callbacks_json = ? WHERE date = ?", (callbacks_json, date))
    conn.close()


def delete_diary(db_path: str, date: str) -> bool:
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute("DELETE FROM diary WHERE date = ?", (date,))
        deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def get_diary_list(db_path: str, limit: int = 30) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT date, entry, callbacks_json, mood_word, edited, created_at FROM diary ORDER BY date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {
            "date": r[0],
            "entry": r[1],
            "callbacks_json": r[2],
            "mood_word": r[3],
            "edited": bool(r[4]),
            "created_at": r[5],
        }
        for r in rows
    ]


def insert_or_update_diary_week(db_path: str, week: str, summary: str, callbacks_json: str) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO diary_weeks (week, summary, callbacks_json) VALUES (?, ?, ?) "
            "ON CONFLICT(week) DO UPDATE SET summary = excluded.summary, "
            "callbacks_json = excluded.callbacks_json",
            (week, summary, callbacks_json),
        )
    conn.close()


def get_diary_weeks(db_path: str, limit: int = 12) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT week, summary, callbacks_json FROM diary_weeks ORDER BY week DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [{"week": w, "summary": s, "callbacks_json": c} for w, s, c in rows]


def get_latest_diary_week(db_path: str) -> dict | None:
    weeks = get_diary_weeks(db_path, limit=1)
    return weeks[0] if weeks else None
