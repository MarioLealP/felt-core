"""The `events` table: the silent desktop-event log (WP-26, POST /v1/events)."""

from felt_core.db.session import _connect, _require_lastrowid


def save_event(db_path: str, kind: str, data_json: str, at_utc: str) -> int:
    """Log one desktop event; returns its id."""
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute("INSERT INTO events (kind, data_json, at) VALUES (?, ?, ?)", (kind, data_json, at_utc))
        event_id = _require_lastrowid(cursor)
    conn.close()
    return event_id


def get_events_between(db_path: str, start_utc: str, end_utc: str) -> list[dict]:
    """Every logged event in [start_utc, end_utc).

    Every logged event whose `at` falls in [start_utc, end_utc) - both
    already-formatted 'YYYY-MM-DD HH:MM:SS' UTC strings, same convention as
    every other timestamp column here, so plain string comparison works.
    """
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT kind, data_json, at FROM events WHERE at >= ? AND at < ? ORDER BY id ASC",
        (start_utc, end_utc),
    ).fetchall()
    conn.close()
    return [{"kind": kind, "data_json": data_json, "at": at} for kind, data_json, at in rows]
