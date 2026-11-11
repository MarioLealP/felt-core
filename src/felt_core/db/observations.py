"""The `screen_observations` table: what she saw on the screen and what she said about it."""

from felt_core.db.session import _connect, _require_lastrowid


def save_observation(db_path: str, session_id: str, description: str) -> int:
    """Save one screen observation and return its id."""
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute(
            "INSERT INTO screen_observations (session_id, description) VALUES (?, ?)",
            (session_id, description),
        )
        obs_id = _require_lastrowid(cursor)
    conn.close()
    return obs_id


def set_observation_reply(db_path: str, obs_id: int, reply: str) -> None:
    """Store the spoken reply on one observation."""
    conn = _connect(db_path)
    with conn:
        conn.execute("UPDATE screen_observations SET spoken_reply = ? WHERE id = ?", (reply, obs_id))
    conn.close()


def get_recent_observations(db_path: str, session_id: str, limit: int = 3) -> list[dict]:
    """The most recent `limit` observations for the session, oldest first."""
    conn = _connect(db_path)
    # DESC + LIMIT then reverse, same "most recent N, chronological order"
    # pattern as get_history above.
    rows = conn.execute(
        "SELECT id, description, spoken_reply, created_at FROM screen_observations "
        "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [
        {
            "id": obs_id,
            "description": description,
            "spoken_reply": spoken_reply,
            "created_at": created_at,
        }
        for obs_id, description, spoken_reply, created_at in reversed(rows)
    ]


def seconds_since_last_spoken_reaction(db_path: str, session_id: str) -> float | None:
    """None if she's never actually spoken a screen reaction in this session.

    the caller (the cooldown check) treats that as "not on cooldown," not as "infinitely
    long ago." Age math done in SQL (julianday) - `created_at` is SQLite's own
    `datetime('now')` UTC string, and comparing it in Python would mean parsing that
    format back out for no benefit.
    """
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT (julianday('now') - julianday(created_at)) * 86400.0 "
        "FROM screen_observations "
        "WHERE session_id = ? AND spoken_reply IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_current_screen_description(db_path: str, session_id: str, max_age_seconds: int) -> str | None:
    """The most recent observation for the session.

    if it's recent enough to still count as "what's currently on screen" (WP-02: normal
    chat gets a "Currently on Mario's screen: ..." system message when this is fresh, so
    "what do you think of this?" has something to refer to).
    """
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT description FROM screen_observations "
        "WHERE session_id = ? AND created_at >= datetime('now', ?) "
        "ORDER BY id DESC LIMIT 1",
        (session_id, f"-{max_age_seconds} seconds"),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_screen_observations_between(db_path: str, start_utc: str, end_utc: str) -> list[dict]:
    """List the screen observations recorded between `start_utc` and `end_utc`."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT description, spoken_reply FROM screen_observations "
        "WHERE created_at >= ? AND created_at < ? ORDER BY id ASC",
        (start_utc, end_utc),
    ).fetchall()
    conn.close()
    return [{"description": d, "spoken_reply": r} for d, r in rows]
