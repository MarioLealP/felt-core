"""WP-42.5: which of her alert lines pop on the desktop, and how."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from felt_core import alert_lines, incidents, notices, responder
from felt_core.api import app

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _in(db, title):
    incident_id = incidents.intake(db, title, at=T0)
    assert incident_id is not None
    return incident_id


async def _no_watch(config) -> None:
    return None


def test_the_rules():
    assert notices.rules("urgent", "line", None, False) == {
        "pop": True,
        "sound": True,
        "sticky": True,
    }
    assert notices.rules("high", "line", None, False) == {
        "pop": True,
        "sound": False,
        "sticky": False,
    }
    assert not notices.rules("default", "line", None, False)["pop"]
    assert not notices.rules("low", "followup", {"verdict": "expected"}, False)["pop"]
    # an outcome that needs him, or Claude couldn't look: always, and it stays
    needs = notices.rules("default", "followup", {"verdict": "needs-mario"}, False)
    assert needs == {"pop": True, "sound": False, "sticky": True}
    assert notices.rules("low", "both", None, True)["sticky"]


def test_her_lines_come_with_what_the_pop_up_needs(tmp_path):
    db = str(tmp_path / "f.db")
    assert notices.latest(db) == 0 and notices.after(db, 0) == []
    honeypot = _in(db, "HONEYPOT: 192.168.10.201 banned")
    trivy = _in(db, "Trivy weekly: 1 critical")
    alert_lines.store(db, honeypot, "Someone poked the honeypot, Claude's on it.")
    alert_lines.store(db, trivy, "The weekly scan found one critical.")
    responder.claim_next(db, T0)
    responder.save_report(db, honeypot, {"verdict": "needs-mario", "summary": "x"}, "s1")
    alert_lines.store(db, honeypot, "Claude wants you to look.", "followup")
    lines = notices.after(db, 0)
    assert [(n["incident_id"], n["kind"], n["pop"]) for n in lines] == [
        (honeypot, "line", True),
        (trivy, "line", False),
        (honeypot, "followup", True),
    ]
    assert lines[0]["area"] == "security" and lines[0]["sound"]
    assert notices.after(db, lines[1]["message_id"]) == lines[2:]
    assert notices.latest(db) == lines[2]["message_id"]


def test_the_tray_routes_need_the_token(tmp_path, monkeypatch):
    # the lifespan's incidents watcher would expire these T0 (2026-10-03)
    # incidents mid-test if its first round lands between intake and the POST
    monkeypatch.setattr(incidents, "watch", _no_watch)
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_API_TOKEN", "s3cret")
    token = {"Authorization": "Bearer s3cret"}
    with TestClient(app) as c:
        assert c.get("/v1/notices").status_code == 401
        assert c.get("/v1/notices?after=0", headers=token).json() == []
        assert c.get("/v1/notices/latest", headers=token).json() == {"id": 0}
        backup = _in(str(tmp_path / "felt.db"), "backup full FAILED")
        assert c.post(f"/v1/incidents/{backup}/handled").status_code == 401
        assert c.post(f"/v1/incidents/{backup}/handled", headers=token).json() == {"ok": True}
        assert c.post(f"/v1/incidents/{backup}/handled", headers=token).status_code == 409
