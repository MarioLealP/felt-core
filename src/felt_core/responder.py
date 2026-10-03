"""WP-58: the handoff between her incidents and the felt-responder, the
headless Claude on the server that investigates read-only.

The chain (Mario 10-03): she tells him "Claude's on it" (alert_lines, her
first line), the responder claims the incident here, investigates, and
posts its report back; she then tells him the outcome in a follow-up line.
If Claude can't look - the responder is down, the daily cap is spent, or no
report comes in time - the incident is marked missed and her follow-up says
so, so nothing is dropped quietly.
"""

import json
from datetime import UTC, datetime, timedelta

from felt_core.db import _connect
from felt_core.incidents import _ensure, _ts, get

# investigations per UTC day: only a safety net against a runaway loop. The
# real limit is the runner's: it skips when Claude's usage is past 70% of the
# 5-hour window or 85% of the week (Mario 10-03: "both numbers look good")
DAILY_CAP = 50
# an incident nobody claimed this long after it opened is missed
CLAIM_WITHIN = timedelta(minutes=15)
# a claimed one with no report this long after is missed (the run caps at 10)
REPORT_WITHIN = timedelta(minutes=20)

VERDICTS = ("expected", "fixed-itself", "needs-mario", "unknown")
# verdicts that need nothing from Mario: the incident is done
_DONE = ("expected", "fixed-itself")

# Claude's turn: open, his to handle, not yet claimed or given up on
_WAITING = "handler = 'claude' AND state = 'open' AND handed_at IS NULL AND missed_at IS NULL"
_TIERS = "CASE tier WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'default' THEN 2 ELSE 3 END"


def waiting(db_path: str) -> int:
    """How many incidents are waiting for Claude - so the runner only checks
    the usage limits when there's work."""
    conn = _connect(db_path)
    try:
        _ensure(conn)
        return conn.execute(f"SELECT COUNT(*) FROM incidents WHERE {_WAITING}").fetchone()[0]
    finally:
        conn.close()


def claim_next(db_path: str, now: datetime | None = None, cap: int = DAILY_CAP) -> dict | None:
    """The next incident for Claude (most urgent, then oldest), marked as
    handed over; None when there's none or today's cap is spent."""
    now = now or datetime.now(UTC)
    day = _ts(now.replace(hour=0, minute=0, second=0, microsecond=0))
    conn = _connect(db_path)
    try:
        _ensure(conn)
        with conn:
            today = conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE handed_at >= ?", (day,)
            ).fetchone()[0]
            if today >= cap:
                return None
            row = conn.execute(
                f"SELECT id FROM incidents WHERE {_WAITING} ORDER BY {_TIERS}, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE incidents SET handed_at = ? WHERE id = ?", (_ts(now), row[0]))
    finally:
        conn.close()
    return get(db_path, row[0])


def save_report(db_path: str, incident_id: int, report: dict, session_id: str) -> bool:
    """Stores Claude's report on a claimed incident; False if it wasn't his
    (unknown, never handed over, or already reported)."""
    verdict = report.get("verdict")
    conn = _connect(db_path)
    try:
        _ensure(conn)
        with conn:
            cursor = conn.execute(
                "UPDATE incidents SET report = ?, session_id = ?, handled_at = datetime('now'),"
                " state = CASE WHEN ? THEN 'handled' ELSE state END"
                " WHERE id = ? AND handed_at IS NOT NULL AND report IS NULL",
                (json.dumps(report), session_id, verdict in _DONE, incident_id),
            )
    finally:
        conn.close()
    return cursor.rowcount == 1


def mark_missed(db_path: str, now: datetime | None = None) -> list[int]:
    """Incidents Claude didn't get to in time: never claimed within
    CLAIM_WITHIN of opening, or claimed with no report within REPORT_WITHIN."""
    now = now or datetime.now(UTC)
    conn = _connect(db_path)
    try:
        _ensure(conn)
        with conn:
            ids = [
                r[0]
                for r in conn.execute(
                    f"SELECT id FROM incidents WHERE ({_WAITING}"
                    " AND COALESCE(due_at, first_at) <= ?)"
                    " OR (handed_at IS NOT NULL AND report IS NULL AND missed_at IS NULL"
                    " AND handed_at <= ?)",
                    (_ts(now - CLAIM_WITHIN), _ts(now - REPORT_WITHIN)),
                )
            ]
            if ids:
                q = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE incidents SET missed_at = ? WHERE id IN ({q})", [_ts(now), *ids]
                )
    finally:
        conn.close()
    return ids
