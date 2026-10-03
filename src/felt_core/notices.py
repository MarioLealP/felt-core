"""WP-42.5: her alert lines as desktop pop-ups - which ones pop, and how.

The tray polls GET /v1/notices?after=<id> (its bearer token) and shows a
pop-up for each line marked `pop`. The rules live here, in one place (Mario
10-03, "Sounds good"):

- urgent: pops, with a sound, and stays until he closes it;
- high: pops quietly and fades;
- default / low: never pops (Systems and the history only);
- except: an outcome that needs him, or "Claude couldn't look", always pops
  and stays, whatever the level.

Her quiet switch (controls.py) doesn't touch these: quiet is about her, and
alerts come through (Mario 10-03: "No this is for felt, raw ntfy can come
through").
"""

import json
import sqlite3

from felt_core.db import ALERT, _connect
from felt_core.incidents import _ensure

_POPS = ("urgent", "high")


def _kind(source_ref: str) -> str:
    parts = source_ref.split(":")
    return parts[2] if len(parts) > 2 else "line"


def rules(tier: str, kind: str, report: dict | None, missed: bool) -> dict:
    """pop / sound / sticky for one of her lines."""
    outcome = kind in ("followup", "both")
    needs_him = outcome and (
        (report is not None and report.get("verdict") in ("needs-mario", "unknown"))
        or (report is None and missed)
    )
    pop = tier in _POPS or needs_him
    return {
        "pop": pop,
        "sound": pop and tier == "urgent",
        "sticky": pop and (tier == "urgent" or needs_him),
    }


def latest(db_path: str) -> int:
    """The newest alert line's id: where a freshly started tray begins."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT MAX(id) FROM messages WHERE kind = ?", (ALERT,)).fetchone()
    except sqlite3.OperationalError:  # no messages table yet
        return 0
    finally:
        conn.close()
    return row[0] or 0


def after(db_path: str, last_id: int, limit: int = 20) -> list[dict]:
    """Her alert lines newer than `last_id`, oldest first, each with what
    the pop-up needs and whether it pops at all."""
    conn = _connect(db_path)
    try:
        _ensure(conn)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT m.id AS message_id, m.content, m.source_ref, m.created_at, i.*"
                " FROM messages m JOIN incidents i"
                " ON i.id = CAST(substr(m.source_ref, 10) AS INTEGER)"
                " WHERE m.kind = ? AND m.id > ? AND m.source_ref LIKE 'incident:%'"
                " ORDER BY m.id LIMIT ?",
                (ALERT, last_id, limit),
            ).fetchall()
        except sqlite3.OperationalError:  # no messages table yet
            return []
    finally:
        conn.close()
    from felt_core.systems import area_of

    out = []
    for r in rows:
        report = json.loads(r["report"]) if r["report"] else None
        kind = _kind(r["source_ref"])
        out.append(
            {
                "message_id": r["message_id"],
                "incident_id": r["id"],
                "text": r["content"],
                "title": r["title"],
                "area": area_of(r["type"], r["title"], r["message"]),
                "tier": r["tier"],
                "kind": kind,
                "at": r["created_at"],
                **rules(r["tier"], kind, report, bool(r["missed_at"])),
            }
        )
    return out
