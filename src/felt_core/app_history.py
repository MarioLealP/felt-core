"""WP-42.2: the Felt app's view of the conversation.

The app's Talk page shows only the real back-and-forth (Mario 10-02: "only
feed, keep actual chat clean"), so her unprompted remarks are left out the
same way get_history(exclude_remarks=True) does. Unlike get_history it returns
ids and times and pages backwards, for a long-running scroll.
"""

from felt_core.db import _REMARK_EXCLUSION_SQL, _connect

MAX_PAGE = 200


def history_page(
    db_path: str, session_id: str, *, before_id: int | None = None, limit: int = 50
) -> list[dict]:
    """Up to `limit` messages older than `before_id` (newest page when None),
    oldest first. Each row: id, role, content, created_at (UTC, ISO-8601)."""
    limit = max(1, min(limit, MAX_PAGE))
    before = "AND id < ? " if before_id is not None else ""
    params: tuple = (session_id,)
    if before_id is not None:
        params += (before_id,)
    params += (session_id, session_id, limit)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE session_id = ? AND content NOT LIKE '[System Observation%' "
            + before
            + _REMARK_EXCLUSION_SQL
            + " ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": i, "role": role, "content": content, "created_at": created.replace(" ", "T") + "Z"}
        for i, role, content, created in reversed(rows)
    ]
