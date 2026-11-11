"""The `latency` table: per-request stage timings."""

from felt_core.db.session import _connect


def save_latency_row(
    db_path: str,
    *,
    request_id: str,
    endpoint: str,
    stage: str,
    ms: float,
    route: str | None = None,
    skill: str | None = None,
    model: str | None = None,
    chars: int | None = None,
    session_id: str | None = None,
) -> None:
    """Store one per-request stage timing row."""
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO latency (request_id, endpoint, stage, ms, route, skill, model, chars, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, endpoint, stage, ms, route, skill, model, chars, session_id),
        )
    conn.close()


def get_latency_rows(db_path: str, since_hours: int = 24, endpoint: str | None = None) -> list[dict]:
    """Stage timings from the last given hours, optionally for one endpoint."""
    conn = _connect(db_path)
    query = "SELECT stage, ms, route, skill FROM latency WHERE created_at >= datetime('now', ?)"
    params: list = [f"-{since_hours} hours"]
    if endpoint:
        query += " AND endpoint = ?"
        params.append(endpoint)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [{"stage": stage, "ms": ms, "route": route, "skill": skill} for stage, ms, route, skill in rows]


def delete_old_latency_rows(db_path: str, older_than_days: int = 30) -> None:
    """Drop latency rows older than the given number of days."""
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "DELETE FROM latency WHERE created_at < datetime('now', ?)",
            (f"-{older_than_days} days",),
        )
    conn.close()
