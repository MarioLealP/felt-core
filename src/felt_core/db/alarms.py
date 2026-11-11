"""The `alarms` table."""

from felt_core.db.session import _connect, _require_lastrowid


def save_alarm(db_path: str, fire_at_iso: str, label: str) -> int:
    """Store one alarm row and return its id."""
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute("INSERT INTO alarms (fire_at, label) VALUES (?, ?)", (fire_at_iso, label))
        alarm_id = _require_lastrowid(cursor)
    conn.close()
    return alarm_id


def get_pending_alarms(db_path: str) -> list[dict]:
    """Return every unfired alarm, ordered by fire time."""
    conn = _connect(db_path)
    rows = conn.execute("SELECT id, fire_at, label FROM alarms WHERE fired = 0 ORDER BY fire_at ASC").fetchall()
    conn.close()
    return [{"id": row[0], "fire_at": row[1], "label": row[2]} for row in rows]


def mark_alarm_fired(db_path: str, alarm_id: int) -> None:
    """Mark one alarm as fired."""
    conn = _connect(db_path)
    with conn:
        conn.execute("UPDATE alarms SET fired = 1 WHERE id = ?", (alarm_id,))
    conn.close()


def delete_alarm(db_path: str, alarm_id: int) -> None:
    """Distinct from mark_alarm_fired: a cancelled alarm never fired.

    Marking it 'fired' would be a lie — this removes the row outright.
    """
    conn = _connect(db_path)
    with conn:
        conn.execute("DELETE FROM alarms WHERE id = ?", (alarm_id,))
    conn.close()
