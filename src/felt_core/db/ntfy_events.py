"""The `ntfy_events` table: what came in over ntfy."""

from felt_core.db.session import _connect, _require_lastrowid


def save_ntfy_event(db_path: str, title: str | None, message: str, priority: int | None) -> int:
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute(
            "INSERT INTO ntfy_events (title, message, priority) VALUES (?, ?, ?)",
            (title, message, priority),
        )
        event_id = _require_lastrowid(cursor)
    conn.close()
    return event_id


def get_recent_ntfy_events(db_path: str, limit: int = 10) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT title, message, priority, received_at FROM ntfy_events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {"title": title, "message": message, "priority": priority, "received_at": received_at}
        for title, message, priority, received_at in rows
    ]
