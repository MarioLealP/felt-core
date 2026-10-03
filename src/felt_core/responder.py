"""WP-58: the handoff between her incidents and the felt-responder, the
headless Claude on the server that investigates read-only.

The chain (Mario 10-03): she tells him "Claude's on it" (alert_lines, her
first line), the responder claims the incident here, investigates, and
posts its report back; she then tells him the outcome in a follow-up line.
If Claude can't look - the responder is down, his usage limit is reached,
or no report comes in time - the incident is marked missed and her follow-up
says so, so nothing is dropped quietly.

Push and pull (Mario 10-03: "just have a system send it to him directly";
"both even, push and pull"): when an incident starts waiting, wake() touches
a marker file next to the database and a systemd path unit on the host
starts a responder round at once - a doorbell that carries no data and opens
no port. The responder's timer still checks every 2 minutes as the backup.
Because pickup is normally instant, the deadlines are short and depend on
how urgent it is.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from felt_core.db import _connect
from felt_core.incidents import _ensure, _ts, get

# investigations per UTC day: only a safety net against a runaway loop. The
# real limit is the runner's: it skips when Claude's usage is past 70% of the
# 5-hour window or 85% of the week (Mario 10-03: "both numbers look good")
DAILY_CAP = 50
# minutes, by tier (Mario 10-03: "Sounds great"): nobody claimed it this long
# after it opened -> missed; claimed but no report this long after -> missed
# (the runner caps Claude at 6 min for urgent, 10 otherwise)
CLAIM_WITHIN = {"urgent": 2, "high": 3, "default": 5, "low": 5}
REPORT_WITHIN = {"urgent": 8, "high": 12, "default": 12, "low": 12}
WAKE_FILE = "responder-wake"

VERDICTS = ("expected", "fixed-itself", "needs-mario", "unknown")
# verdicts that need nothing from Mario: the incident is done
_DONE = ("expected", "fixed-itself")

# Claude's turn: open, his to handle, not yet claimed or given up on
_WAITING = "handler = 'claude' AND state = 'open' AND handed_at IS NULL AND missed_at IS NULL"
_TIERS = "CASE tier WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'default' THEN 2 ELSE 3 END"


def _minutes(by_tier: dict[str, int]) -> str:
    """SQL for a tier's deadline in minutes."""
    whens = " ".join(f"WHEN '{tier}' THEN {m}" for tier, m in by_tier.items())
    return f"CASE tier {whens} ELSE {max(by_tier.values())} END"


def _age(column: str) -> str:
    """SQL for minutes since `column`, against the ? that is now."""
    # whole seconds: julianday's floats make exactly 2 minutes 1.99999...
    return f"(strftime('%s', ?) - strftime('%s', {column})) / 60.0"


def wake(db_path: str) -> bool:
    """Rings the responder's doorbell if anything waits for Claude: rewrites
    a marker file next to the database, which the host's path unit watches."""
    if not waiting(db_path):
        return False
    Path(db_path).with_name(WAKE_FILE).write_text(datetime.now(UTC).isoformat())
    return True


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
    """Incidents Claude didn't get to in time, by tier: never claimed within
    CLAIM_WITHIN of opening, or claimed with no report within REPORT_WITHIN."""
    stamp = _ts(now or datetime.now(UTC))
    conn = _connect(db_path)
    try:
        _ensure(conn)
        with conn:
            ids = [
                r[0]
                for r in conn.execute(
                    f"SELECT id FROM incidents WHERE ({_WAITING}"
                    f" AND {_age('COALESCE(due_at, first_at)')} >= {_minutes(CLAIM_WITHIN)})"
                    " OR (handed_at IS NOT NULL AND report IS NULL AND missed_at IS NULL"
                    f" AND {_age('handed_at')} >= {_minutes(REPORT_WITHIN)})",
                    (stamp, stamp),
                )
            ]
            if ids:
                q = ",".join("?" * len(ids))
                conn.execute(f"UPDATE incidents SET missed_at = ? WHERE id IN ({q})", [stamp, *ids])
    finally:
        conn.close()
    return ids
