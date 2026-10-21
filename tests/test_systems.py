"""WP-42.5: the Systems page - areas from the incidents, the one circled,
history, details with Claude's trail, and "handled" from the app."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from felt_core import alert_lines, incidents, responder, systems
from felt_core.api import app
from felt_core.asides import remark_text

SSO = {"X-Authentik-Username": "mario"}
T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
REPORT = {
    "verdict": "needs-mario",
    "summary": "Someone touched the honeypot.",
    "trail": [{"tool": "Bash", "input": "clockmost-read logs ...", "output": "1 row"}],
}


def _in(db, title, message="", *, minutes=0):
    incident_id = incidents.intake(db, title, message, at=T0 + timedelta(minutes=minutes))
    assert incident_id is not None
    return incident_id


async def _no_watch(config) -> None:
    return None


def test_each_incident_lands_in_its_area():
    assert systems.area_of("honeypot", "", "") == "security"
    assert systems.area_of("backup-failed", "", "") == "backups"
    assert systems.area_of("host-load", "", "Host: clockmost-windows / ...") == "desktop"
    assert systems.area_of("disk-space", "", "Host: clockmost-nitro5") == "laptop"
    assert systems.area_of("router-network", "", "Host: r7000") == "router"
    assert systems.area_of("container-down", "", "Host: clockmost") == "server"
    assert systems.area_of("pc-agent", "", "") == "desktop"


def test_areas_are_fine_unless_something_needs_him_and_the_worst_is_circled(tmp_path):
    db = str(tmp_path / "f.db")
    _in(db, "clockmost: update deployed", "immich - verified + healthy")  # noted
    trivy = _in(db, "Trivy weekly: 3 critical")  # Claude's, not claimed yet
    honeypot = _in(db, "HONEYPOT: 192.168.10.201 banned", minutes=1)
    responder.claim_next(db, T0 + timedelta(minutes=2))  # honeypot (urgent) first
    responder.save_report(db, honeypot, REPORT, "s1")
    page = systems.page(db)
    by_area = {a["name"]: a for a in page["areas"]}
    assert by_area["security"]["status"] == "needs-you"
    assert by_area["security"]["incident_id"] == honeypot and by_area["security"]["count"] == 2
    assert by_area["server"]["status"] == "fine" and by_area["backups"]["status"] == "fine"
    assert page["circled"] == honeypot
    assert [i["id"] for i in page["incidents"]][:2] == [honeypot, trivy]
    statuses = {i["id"]: i["status"] for i in page["incidents"]}
    assert statuses[trivy] == "looking" and statuses[honeypot] == "needs-you"


def test_dropped_noise_never_shows(tmp_path):
    db = str(tmp_path / "f.db")
    _in(db, 'clockmost Average: Windows: "AppXSvc" is not running')
    assert systems.page(db)["incidents"] == []


def test_details_carry_the_report_with_its_trail_and_the_raw_alerts(tmp_path):
    db = str(tmp_path / "f.db")
    honeypot = _in(db, "HONEYPOT: 192.168.10.201 banned", "Telnet connect on port 23")
    responder.claim_next(db, T0)
    responder.save_report(db, honeypot, REPORT, "s1")
    found = systems.details(db, honeypot)
    assert found is not None
    assert found["report"]["trail"][0]["input"].startswith("clockmost-read")
    assert found["alerts"][0]["message"] == "Telnet connect on port 23"
    assert found["session_id"] == "s1" and "zabbix" in found["links"]


def test_handled_from_the_app_closes_it(tmp_path):
    db = str(tmp_path / "f.db")
    backup = _in(db, "backup full FAILED")
    assert systems.mark_handled(db, backup)
    assert not systems.mark_handled(db, backup)  # only once
    found = systems.details(db, backup)
    assert found is not None and found["state"] == "handled" and found["handled_by"] == "mario"
    assert systems.page(db)["circled"] is None


def test_routes_need_the_sso_header(tmp_path, monkeypatch):
    # the lifespan's incidents watcher would expire these T0 (2026-10-03)
    # incidents mid-test if its first round lands between intake and the POST
    monkeypatch.setattr(incidents, "watch", _no_watch)
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_API_TOKEN", "s3cret")
    with TestClient(app) as c:
        assert c.get("/v1/app/systems").status_code == 401
        page = c.get("/v1/app/systems", headers=SSO).json()
        assert [a["name"] for a in page["areas"]] == list(systems.AREAS)
        backup = _in(str(tmp_path / "felt.db"), "backup full FAILED")
        assert c.get(f"/v1/app/incidents/{backup}", headers=SSO).json()["id"] == backup
        assert c.get("/v1/app/incidents/999", headers=SSO).status_code == 404
        assert c.post(f"/v1/app/incidents/{backup}/handled").status_code == 401
        assert c.post(f"/v1/app/incidents/{backup}/handled", headers=SSO).json() == {"ok": True}
        assert c.post(f"/v1/app/incidents/{backup}/handled", headers=SSO).status_code == 409


def test_ask_felt_answers_her_alert_line_with_what_claude_found(tmp_path):
    db = str(tmp_path / "f.db")
    honeypot = _in(db, "HONEYPOT: 192.168.10.201 banned")
    alert_lines.store(db, honeypot, "Someone poked the honeypot, Claude's on it.")
    responder.claim_next(db, T0)
    responder.save_report(db, honeypot, REPORT, "s1")
    alert_lines.store(db, honeypot, "Claude wants you to look at it.", "followup")
    line_id = systems.page(db)["incidents"][0]["line_message_id"]
    assert line_id is not None
    text = remark_text(db, line_id)
    assert text is not None
    assert text.startswith("Claude wants you to look at it.")
    assert "HONEYPOT: 192.168.10.201 banned" in text and "needs-mario" in text
    assert "Someone touched the honeypot." in text
