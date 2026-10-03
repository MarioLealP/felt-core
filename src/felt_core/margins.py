"""WP-42.3: the Margins page - everything she said that isn't the conversation
itself (remarks, event lines, pushes, mail, alarms), newest last, with read /
dismissed / snoozed state on the message rows (db.py's WP-42.3 columns).

Mario 10-02: Talk stays "only the conversation"; her chatter lives here.
"""

from datetime import UTC, datetime, timedelta

from felt_core.db import ALERT, APP_SESSION, CHAT, _connect

MAX_PAGE = 200
MAX_SNOOZE_HOURS = 24 * 7
ACTIONS = ("read", "dismiss", "snooze")

# hidden: dismissed, or snoozed until later than now
_VISIBLE_SQL = (
    f"session_id = ? AND role = 'assistant' AND kind NOT IN ('{CHAT}', '{ALERT}')"
    " AND dismissed_at IS NULL"
    " AND (snoozed_until IS NULL OR snoozed_until <= datetime('now'))"
)


def _source(kind: str, source_ref: str | None) -> str:
    """A short label for where the line came from: the event name for event
    lines ("song", "guest"), the push kind for pushes ("diary",
    "calendar_15"), else the kind ("remark", "email", "alarm")."""
    parts = (source_ref or "").split(":")
    if kind in ("event", "proactive") and len(parts) >= 2 and parts[1]:
        return parts[1]
    return kind


def _iso(ts: str | None) -> str | None:
    return ts.replace(" ", "T") + "Z" if ts else None


def page(
    db_path: str, session_id: str = APP_SESSION, *, before_id: int | None = None, limit: int = 50
) -> list[dict]:
    """Up to `limit` visible lines older than `before_id` (newest page when
    None), oldest first."""
    limit = max(1, min(limit, MAX_PAGE))
    before = " AND id < ?" if before_id is not None else ""
    params: tuple = (session_id,) + ((before_id,) if before_id is not None else ()) + (limit,)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, source_ref, content, created_at, read_at FROM messages WHERE "
            + _VISIBLE_SQL
            + before
            + " ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": i,
            "kind": kind,
            "source": _source(kind, ref),
            "content": content,
            "created_at": _iso(created),
            "read": read_at is not None,
        }
        for i, kind, ref, content, created, read_at in reversed(rows)
    ]


def unread(db_path: str, session_id: str = APP_SESSION) -> int:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE " + _VISIBLE_SQL + " AND read_at IS NULL",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0]


def mark(
    db_path: str,
    ids: list[int],
    action: str,
    *,
    hours: float | None = None,
    session_id: str = APP_SESSION,
) -> int:
    """Applies `action` to those of `ids` that are her non-chat lines in this
    session; returns how many changed. Dismissing also marks read; snoozing
    clears it, so the line comes back unread, to be noticed again."""
    if action not in ACTIONS or not ids:
        return 0
    now = datetime.now(UTC)
    if action == "read":
        sets, values = "read_at = COALESCE(read_at, ?)", (now,)
    elif action == "dismiss":
        sets, values = "dismissed_at = ?, read_at = COALESCE(read_at, ?)", (now, now)
    else:
        span = min(max(hours or 0, 0.25), MAX_SNOOZE_HOURS)
        until = now + timedelta(hours=span)
        sets, values = "snoozed_until = ?, read_at = NULL", (until,)
    stamp = tuple(v.strftime("%Y-%m-%d %H:%M:%S") for v in values)
    q = ",".join("?" * len(ids))
    conn = _connect(db_path)
    try:
        with conn:
            cursor = conn.execute(
                f"UPDATE messages SET {sets} WHERE id IN ({q}) AND session_id = ?"
                f" AND role = 'assistant' AND kind NOT IN ('{CHAT}', '{ALERT}')",
                (*stamp, *ids, session_id),
            )
    finally:
        conn.close()
    return cursor.rowcount
