"""The `messages` table: chat turns and every line she says (kinds in db/kinds.py)."""

from felt_core.db.kinds import ASIDE_KINDS, CHAT
from felt_core.db.session import _connect, _require_lastrowid


def save_message(
    db_path: str,
    session_id: str,
    role: str,
    content: str,
    kind: str = CHAT,
    source_ref: str | None = None,
) -> int:
    """Store one message row and return its id."""
    conn = _connect(db_path)
    with conn:
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, kind, source_ref) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, kind, source_ref),
        )
        message_id = _require_lastrowid(cursor)
    conn.close()
    return message_id


# Only the conversation itself: her unprompted lines (remarks, event lines,
# and from WP-42.3 on, pushes) live in `messages` too, labelled by `kind`.
CHAT_ONLY_SQL = f" AND kind = '{CHAT}'"


def get_history(db_path: str, session_id: str, limit: int = 20, exclude_remarks: bool = False) -> list[dict]:
    """`exclude_remarks=True` drops her own unprompted lines (kind other than chat).

    Leaving only the real back-and-forth - what the local screen-reaction
    path wants as "recent conversation" (audit 2026-09-24: it was feeding
    her earlier screen remarks back to her).
    """
    conn = _connect(db_path)
    # ORDER BY id DESC so LIMIT keeps the MOST RECENT messages (once a
    # session passes the limit, ASC would keep returning the oldest ones
    # forever, permanently hiding anything recent — 2026-09-14 review
    # finding). Reverse back to chronological order before returning.
    #
    # Excludes legacy "[System Observation" rows (2026-09-22, WP-02):
    # screen observations used to be saved into this same table as fake
    # `user` messages, before screen_observations got its own table. This
    # is a non-destructive fix for rows already in the prod DB - it hides
    # them from both the brain's own context and /v1/history without
    # deleting anything.
    exclusion = CHAT_ONLY_SQL if exclude_remarks else ""
    rows = conn.execute(
        "SELECT role, content FROM messages "  # ruff: ignore[hardcoded-sql-expression]  # constant SQL: only ? placeholders and fixed columns go in
        "WHERE session_id = ? AND content NOT LIKE '[System Observation%'" + exclusion + " "
        "ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def get_her_recent_lines(db_path: str, session_id: str, limit: int = 8, max_age: str = "-6 hours") -> list[dict]:
    """Her own unprompted lines: the newest `limit` within `max_age`, oldest first.

    Kinds: remarks, event lines, pushes, mail; not alarms, which are his own
    labels. Fed back to her so she knows what she already said outside the
    conversation and stops repeating it (Mario 10-03).
    """
    kinds = ",".join(f"'{k}'" for k in ASIDE_KINDS)
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT content, created_at FROM messages WHERE session_id = ? AND role = 'assistant'"  # ruff: ignore[hardcoded-sql-expression]  # constant SQL: only ? placeholders and fixed columns go in
        f" AND kind IN ({kinds}) AND created_at >= datetime('now', ?)"
        " ORDER BY id DESC LIMIT ?",
        (session_id, max_age, limit),
    ).fetchall()
    conn.close()
    return [{"content": c, "created_at": t} for c, t in reversed(rows)]


def get_message_sessions_between(db_path: str, start_utc: str, end_utc: str) -> list[list[dict]]:
    """Real conversation (not her own unprompted remarks) in the window.

    Grouped by session - the diary's material gatherer treats each
    session's text as a WHOLE window, so sessions must come back
    separately, not pre-flattened.
    """
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT m.session_id, m.role, m.content FROM messages m "  # ruff: ignore[hardcoded-sql-expression]  # constant SQL: only ? placeholders and fixed columns go in
        "WHERE m.created_at >= ? AND m.created_at < ? "
        "AND m.content NOT LIKE '[System Observation%' "
        f"AND m.kind = '{CHAT}' ORDER BY m.id ASC",
        (start_utc, end_utc),
    ).fetchall()
    conn.close()
    sessions: dict[str, list[dict]] = {}
    for session_id, role, content in rows:
        sessions.setdefault(session_id, []).append({"role": role, "content": content})
    return list(sessions.values())
