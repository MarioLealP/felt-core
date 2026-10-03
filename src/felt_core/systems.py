"""WP-42.5: the Systems page's data - how things are, and what happened.

Built from the incidents only (WP-44/58), so the page shows what she and
Claude concluded rather than a second, noisier source of truth (Mario 10-03:
"sounds good, but let me click on the incident and see further").

Areas: a handful of places an incident can be about. Each is fine unless an
open incident there needs Mario; the one most worth his attention gets the
raspberry circle in the app.
"""

import json
import re
import sqlite3

from felt_core.alert_types import by_name
from felt_core.db import _connect
from felt_core.incidents import _ensure, get

AREAS = ("server", "backups", "security", "desktop", "laptop", "router")
_SECURITY = {"wazuh-fim", "wazuh-oom", "honeypot", "authentik-bruteforce", "crowdsec", "trivy"}
_BACKUPS = {"backup-failed", "updates-skipped", "storage-check", "pre-update-backup"}
_BY_TYPE = {
    "pc-agent": "desktop",
    "windows-crash": "desktop",
    "windows-service": "desktop",
    "nitro-ollama": "laptop",
    "nitro-gpu-hot": "laptop",
}
# Zabbix "Host: x" / Wazuh "agent: x" -> area
_BY_HOST = (
    (re.compile(r"clockmost-(windows|desktop|linux)\b"), "desktop"),
    (re.compile(r"clockmost-(nitro5|laptop)\b"), "laptop"),
    (re.compile(r"\br7000\b"), "router"),
)
_HOST = re.compile(r"(?:Host|agent): ([\w.-]+)")
_TIERS = ("urgent", "high", "default", "low")
# what the dot says, worst first
STATUSES = ("needs-you", "looking", "watching", "fine")
LINKS = {"zabbix": "https://zabbix.clockmost.com", "wazuh": "https://wazuh.clockmost.com"}


def area_of(incident_type: str, title: str, message: str) -> str:
    if incident_type in _SECURITY:
        return "security"
    if incident_type in _BACKUPS:
        return "backups"
    if incident_type in _BY_TYPE:
        return _BY_TYPE[incident_type]
    host = _HOST.search(message or "")
    for pattern, area in _BY_HOST:
        if host and pattern.search(host.group(1)):
            return area
    if incident_type == "router-network":
        return "router"
    return "server"


def status_of(row: dict) -> str:
    """What an incident means for its area's dot."""
    if row["state"] not in ("open", "pending"):
        return "fine"
    if row["state"] == "pending":
        return "watching"  # waiting to see if it fixes itself
    report = json.loads(row["report"]) if row.get("report") else None
    if report:
        return "needs-you" if report.get("verdict") in ("needs-mario", "unknown") else "fine"
    if row["handler"] == "claude" and not row.get("missed_at"):
        return "looking"
    return "needs-you"  # hers to tell, or Claude couldn't look


def _row(r: sqlite3.Row | dict) -> dict:
    row = dict(r)
    report = json.loads(row["report"]) if row.get("report") else None
    return {
        "id": row["id"],
        "type": row["type"],
        "area": area_of(row["type"], row["title"], row["message"]),
        "tier": row["tier"],
        "state": row["state"],
        "status": status_of(row),
        "expected": by_name(row["type"]).expected,
        "title": row["title"],
        "count": row["count"],
        "first_at": row["first_at"],
        "last_at": row["last_at"],
        "line": row["line"],
        "followup": row["followup"],
        "back_line": row["back_line"],
        "verdict": report.get("verdict") if report else None,
        "missed": bool(row.get("missed_at")),
    }


def page(db_path: str, before: int | None = None, limit: int = 30) -> dict:
    """The areas (from every live incident) and one page of history,
    newest first. Dropped noise never shows."""
    conn = _connect(db_path)
    try:
        _ensure(conn)
        conn.row_factory = sqlite3.Row
        live = [
            _row(r)
            for r in conn.execute("SELECT * FROM incidents WHERE state IN ('open', 'pending')")
        ]
        rows = conn.execute(
            "SELECT * FROM incidents WHERE state != 'dropped' AND id < ? ORDER BY id DESC LIMIT ?",
            (before or 2**62, limit),
        ).fetchall()
    finally:
        conn.close()
    areas = []
    for name in AREAS:
        mine = [i for i in live if i["area"] == name and i["status"] != "fine"]
        mine.sort(key=lambda i: (STATUSES.index(i["status"]), _TIERS.index(i["tier"]), -i["id"]))
        areas.append(
            {
                "name": name,
                "status": mine[0]["status"] if mine else "fine",
                "count": len(mine),
                "incident_id": mine[0]["id"] if mine else None,
            }
        )
    needing = [i for i in live if i["status"] == "needs-you"]
    needing.sort(key=lambda i: (_TIERS.index(i["tier"]), -i["id"]))
    return {
        "areas": areas,
        "circled": needing[0]["id"] if needing else None,
        "incidents": [_row(r) for r in rows],
    }


def details(db_path: str, incident_id: int) -> dict | None:
    """Everything about one incident: her lines, Claude's full report with
    his trail, the raw alerts, the session to join, links out."""
    incident = get(db_path, incident_id)
    if incident is None or incident["state"] == "dropped":
        return None
    out = _row(incident)
    out["alerts"] = incident["alerts"]
    out["report"] = json.loads(incident["report"]) if incident.get("report") else None
    out["session_id"] = incident.get("session_id")
    out["handled_at"] = incident.get("handled_at")
    out["handled_by"] = incident.get("handled_by")
    out["links"] = LINKS
    return out


def mark_handled(db_path: str, incident_id: int) -> bool:
    """Mario says it's dealt with: the incident closes, and nothing more
    is said about it."""
    conn = _connect(db_path)
    try:
        _ensure(conn)
        with conn:
            cursor = conn.execute(
                "UPDATE incidents SET state = 'handled', handled_at = datetime('now'),"
                " handled_by = 'mario' WHERE id = ? AND state IN ('open', 'pending')",
                (incident_id,),
            )
    finally:
        conn.close()
    return cursor.rowcount == 1
