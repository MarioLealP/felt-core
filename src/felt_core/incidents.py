"""WP-44 step 1: every alert on the clockmost ntfy topic becomes an incident.

An incident is one problem, however many alerts it takes: repeats fold into
it (a count), a RESOLVED closes it, and an alert that only ever follows
another one joins it (updates SKIPPED after a failed backup). Self-fixing
types (alert_types.wait_s) start `pending` and only become `open` if their
RESOLVED hasn't come by then; one that resolves first is `cleared` and was
never worth telling.

States:
  pending  - waiting to see if it fixes itself
  open     - real: she tells him; Claude's if its handler says so
  resolved - came back on its own (or was fixed) after it counted
  cleared  - resolved before it counted
  noted    - an expected event: told, never acted on (Mario 10-03)
  dropped  - shouldn't be sent at all (WP-41 noise)
  handled  - Claude looked and it needs nothing from Mario (WP-58), or
             Mario marked it handled in the app (WP-42.5)

Fixed rules only. Step 2 adds her line, WP-58 Claude's report, 42.5 the
Systems page.
"""

import asyncio
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from felt_core import llm_gate
from felt_core.alert_types import AlertType, Parsed, by_name, parse
from felt_core.db import _connect

logger = logging.getLogger(__name__)

LIVE = ("pending", "open")
# a RESOLVED only closes an incident this recent; older ones were missed
_RESOLVE_WINDOW = timedelta(days=7)
# an alert that joins another only joins one this recent
_JOIN_WINDOW = timedelta(minutes=30)
_FMT = "%Y-%m-%d %H:%M:%S"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL,
    handler TEXT NOT NULL,
    state TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    due_at TEXT,
    resolved_at TEXT,
    handled_at TEXT
);
CREATE INDEX IF NOT EXISTS incidents_live ON incidents (state, key);
CREATE TABLE IF NOT EXISTS incident_alerts (
    incident_id INTEGER NOT NULL,
    ntfy_event_id INTEGER,
    title TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
"""


def _ts(at: datetime) -> str:
    return at.astimezone(UTC).strftime(_FMT)


_ADDED_COLUMNS = (
    # step 2 (alert_lines.py): her line, when, and her "it's back"
    ("line", "TEXT"),
    ("line_at", "TEXT"),
    ("back_line", "TEXT"),
    # WP-58 (responder.py): handed to Claude, his report (JSON) and session,
    # or the time she gave up waiting; then her follow-up line
    ("handed_at", "TEXT"),
    ("report", "TEXT"),
    ("session_id", "TEXT"),
    ("missed_at", "TEXT"),
    ("followup", "TEXT"),
    # WP-42.5 (systems.py): who closed it ("mario" from the app)
    ("handled_by", "TEXT"),
    # WP-58: why Claude didn't look - "late" or "off" (the kill switch)
    ("missed_why", "TEXT"),
    # who is investigating once handed over: NULL = Claude, "felt" = her own model on the
    # server when Claude couldn't (its own, longer report deadline)
    ("investigator", "TEXT"),
)


def _ensure(conn) -> None:
    conn.executescript(_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(incidents)")}
    for name, kind in _ADDED_COLUMNS:
        if name not in have:
            try:
                conn.execute(f"ALTER TABLE incidents ADD COLUMN {name} {kind}")
            except sqlite3.OperationalError as exc:
                # another connection added it between the check and here
                # (same tolerance as marks.py / db.py's migration)
                if "duplicate column" not in str(exc):
                    raise
    conn.commit()


def _first_state(alert_type: AlertType) -> str:
    if alert_type.handler == "drop":
        return "dropped"
    if alert_type.expected:
        return "noted"
    return "pending" if alert_type.wait_s else "open"


def intake(
    db_path: str,
    title: str | None,
    message: str = "",
    *,
    at: datetime | None = None,
    ntfy_event_id: int | None = None,
) -> int | None:
    """Files one alert; returns the incident it landed in (None for a
    RESOLVED that matched nothing - the problem it answers predates us)."""
    now = at or datetime.now(UTC)
    parsed = parse(title)
    conn = _connect(db_path)
    try:
        _ensure(conn)
        with conn:
            incident_id = (
                _resolve(conn, parsed, now)
                if parsed.resolves
                else _file(conn, parsed, title or "", message, now)
            )
            if incident_id is not None:
                conn.execute(
                    "INSERT INTO incident_alerts (incident_id, ntfy_event_id, title, message, at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (incident_id, ntfy_event_id, title or "", message, _ts(now)),
                )
    finally:
        conn.close()
    return incident_id


def _file(conn, parsed: Parsed, title: str, message: str, now: datetime) -> int:
    alert_type = parsed.type
    if alert_type.joins:
        row = conn.execute(
            "SELECT id FROM incidents WHERE type = ? AND last_at >= ? ORDER BY id DESC LIMIT 1",
            (alert_type.joins, _ts(now - _JOIN_WINDOW)),
        ).fetchone()
        if row:
            conn.execute("UPDATE incidents SET last_at = ? WHERE id = ?", (_ts(now), row[0]))
            return row[0]
    # a repeat: same alert again within the fold window, whatever its state
    # (a flapping check that resolved and fired again is still one problem)
    row = conn.execute(
        # an expected run she already told him about is closed: the next
        # update is news of its own. Same once Claude has reported on it (or
        # was missed): a new alert after his look must get a look of its own,
        # never fold silently into something already called fine (WP-58)
        "SELECT id, state FROM incidents WHERE key = ? AND state != 'dropped' AND last_at >= ?"
        " AND NOT (state = 'noted' AND line IS NOT NULL)"
        " AND report IS NULL AND missed_at IS NULL ORDER BY id DESC LIMIT 1",
        (parsed.key, _ts(now - timedelta(seconds=alert_type.fold_s))),
    ).fetchone()
    if row:
        incident_id, state = row
        due = None
        if state == "resolved":
            # it counted before and came back: real straight away
            state = "open"
        elif state == "cleared":
            # it fixed itself before and is back: give it the wait again
            # (a check that flaps on every deploy shouldn't escalate)
            state = "pending"
            due = _ts(now + timedelta(seconds=alert_type.wait_s))
        conn.execute(
            "UPDATE incidents SET count = count + 1, last_at = ?, message = ?, state = ?,"
            " due_at = COALESCE(?, due_at),"
            " resolved_at = CASE WHEN ? IN ('open', 'pending') THEN NULL ELSE resolved_at END"
            " WHERE id = ?",
            (_ts(now), message, state, due, state, incident_id),
        )
        return incident_id
    state = _first_state(alert_type)
    due = _ts(now + timedelta(seconds=alert_type.wait_s)) if state == "pending" else None
    cursor = conn.execute(
        "INSERT INTO incidents (type, key, title, message, tier, handler, state, first_at,"
        " last_at, due_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            alert_type.name,
            parsed.key,
            title,
            message,
            alert_type.tier,
            alert_type.handler,
            state,
            _ts(now),
            _ts(now),
            due,
        ),
    )
    return cursor.lastrowid


def _resolve(conn, parsed: Parsed, now: datetime) -> int | None:
    row = conn.execute(
        "SELECT id, state FROM incidents WHERE key = ? AND state IN ('pending', 'open')"
        " AND last_at >= ? ORDER BY id DESC LIMIT 1",
        (parsed.key, _ts(now - _RESOLVE_WINDOW)),
    ).fetchone()
    if row is None:
        return None
    incident_id, state = row
    conn.execute(
        "UPDATE incidents SET state = ?, resolved_at = ?, last_at = ? WHERE id = ?",
        ("cleared" if state == "pending" else "resolved", _ts(now), _ts(now), incident_id),
    )
    return incident_id


def promote_due(db_path: str, now: datetime | None = None) -> list[int]:
    """Pending incidents whose wait ran out without a RESOLVED become open;
    returns their ids (step 2 tells him about them)."""
    stamp = _ts(now or datetime.now(UTC))
    conn = _connect(db_path)
    try:
        _ensure(conn)
        with conn:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM incidents WHERE state = 'pending' AND due_at <= ?", (stamp,)
                )
            ]
            if ids:
                q = ",".join("?" * len(ids))
                conn.execute(f"UPDATE incidents SET state = 'open' WHERE id IN ({q})", ids)
    finally:
        conn.close()
    return ids


# Mario 10-18: a missed one shouldn't wait on him forever - 162 piled up while
# the responder was out of quota or down. Unseen this long, it expires...
EXPIRE_UNSEEN_H = 48
# ...unless it's an attack ("aside from actual we been hacked and someone has
# been hammering our ports"); one still happening keeps its last_at fresh anyway.
# A credential used from an unexpected place counts as one; and reminders meant
# for him ("Zeek decision due") would be pointless if they lapsed
_ATTACK_TYPES = ("honeypot", "authentik-bruteforce", "crowdsec")
_ATTACK_TITLE = re.compile(r"prob(e|ing)|brute|intrusion|attack|unexpected|\bdue$", re.IGNORECASE)


def expire_unseen(db_path: str, now: datetime | None = None) -> list[int]:
    """Open incidents that only wait on Mario by default - Claude missed them,
    or they're hers to tell - and haven't recurred in EXPIRE_UNSEEN_H expire.
    Claude's own "needs Mario" / "unknown" reports never expire."""
    at = now or datetime.now(UTC)
    cutoff = _ts(at - timedelta(hours=EXPIRE_UNSEEN_H))
    q_types = ",".join("?" * len(_ATTACK_TYPES))
    conn = _connect(db_path)
    try:
        _ensure(conn)
        with conn:
            rows = conn.execute(
                "SELECT id, title FROM incidents WHERE state = 'open' AND report IS NULL"
                " AND (handler = 'felt' OR missed_at IS NOT NULL)"
                f" AND last_at <= ? AND type NOT IN ({q_types})",
                (cutoff, *_ATTACK_TYPES),
            ).fetchall()
            ids = [r[0] for r in rows if not _ATTACK_TITLE.search(r[1])]
            if ids:
                q = ",".join("?" * len(ids))
                conn.execute(
                    "UPDATE incidents SET state = 'expired', handled_at = ?,"
                    f" handled_by = 'expiry' WHERE id IN ({q})",
                    [_ts(at), *ids],
                )
    finally:
        conn.close()
    return ids


def get(db_path: str, incident_id: int) -> dict | None:
    conn = _connect(db_path)
    try:
        _ensure(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if row is None:
            return None
        alerts = conn.execute(
            "SELECT title, message, at FROM incident_alerts WHERE incident_id = ? ORDER BY rowid",
            (incident_id,),
        ).fetchall()
    finally:
        conn.close()
    incident = dict(row)
    incident["alerts"] = [dict(a) for a in alerts]
    incident["expected"] = by_name(incident["type"]).expected
    return incident


PROMOTE_EVERY_S = 60.0


async def watch(config) -> None:
    """Background task: turns pending incidents whose wait ran out into
    open ones, marks the ones Claude didn't get to (responder.py), then
    writes the lines she owes (alert_lines.py)."""
    from felt_core import alert_lines, responder

    while True:
        try:
            promoted = await asyncio.to_thread(promote_due, config.db_path)
            if promoted:
                logger.info("incidents: %s now open (no RESOLVED in time)", promoted)
                await asyncio.to_thread(responder.wake, config.db_path)
            missed = await asyncio.to_thread(responder.mark_missed, config.db_path)
            if missed:
                logger.warning("incidents: Claude didn't get to %s in time", missed)
            expired = await asyncio.to_thread(expire_unseen, config.db_path)
            if expired:
                logger.info("incidents: %s expired (unseen %sh)", expired, EXPIRE_UNSEEN_H)
            with llm_gate.timely("alert line"):
                await alert_lines.write_due(config)
        except Exception:
            logger.exception("incidents: round failed")
        await asyncio.sleep(PROMOTE_EVERY_S)
