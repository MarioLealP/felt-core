"""The `event_reactions` table: her lines about desktop events."""

from felt_core.db.session import _connect


def save_event_reaction(db_path: str, session_id: str, event: str, dedupe_key: str | None, line: str) -> None:
    """Store one spoken event line with its event type and dedupe key."""
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO event_reactions (session_id, event, dedupe_key, line) VALUES (?, ?, ?, ?)",
            (session_id, event, dedupe_key, line),
        )
    conn.close()


def seconds_since_last_event_reaction(
    db_path: str, event: str | None = None, dedupe_key: str | None = None
) -> float | None:
    """Age of the most recent spoken event line.

    Any event (event=None), one event type, or one event+dedupe_key (e.g. the same track). None if there's never been
    one. Global across sessions on purpose: there's one Felt, and cooldowns are about how often SHE talks.
    """
    query = "SELECT (julianday('now') - julianday(created_at)) * 86400.0 FROM event_reactions WHERE 1=1"
    params: list = []
    if event is not None:
        query += " AND event = ?"
        params.append(event)
    if dedupe_key is not None:
        query += " AND dedupe_key = ?"
        params.append(dedupe_key)
    conn = _connect(db_path)
    row = conn.execute(query + " ORDER BY id DESC LIMIT 1", params).fetchone()
    conn.close()
    return row[0] if row else None


def last_event_reaction_line(db_path: str, event: str) -> str | None:
    """The most recent spoken line for one event type (None if never).

    Lets an event avoid saying the exact same thing twice in a row.
    """
    conn = _connect(db_path)
    row = conn.execute("SELECT line FROM event_reactions WHERE event = ? ORDER BY id DESC LIMIT 1", (event,)).fetchone()
    conn.close()
    return row[0] if row else None
