"""WP-58: the handoff to the felt-responder - claim, report, missed - her
follow-up line, and the responder token that only opens its two routes."""

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from felt_core import alert_lines, incidents, responder
from felt_core.api import app

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
REPORT = {
    "verdict": "expected",
    "summary": "The deploy of commit abc123 changed caddy's config.",
    "for_mario": "",
}


def _at(minutes):
    return T0 + timedelta(minutes=minutes)


def _in(db, title, *, at=T0):
    incident_id = incidents.intake(db, title, at=at)
    assert incident_id is not None
    return incident_id


def _get(db, incident_id):
    incident = incidents.get(db, incident_id)
    assert incident is not None
    return incident


def test_claims_the_most_urgent_first_and_only_once(tmp_path):
    db = str(tmp_path / "f.db")
    trivy = _in(db, "Trivy: 3 new HIGH vulnerabilities")  # default tier
    _in(db, "clockmost: update deployed")  # expected, Felt's
    honeypot = _in(db, "HONEYPOT: ssh touched")  # urgent
    assert responder.waiting(db) == 2
    first = responder.claim_next(db, _at(1))
    assert first is not None and first["id"] == honeypot
    second = responder.claim_next(db, _at(1))
    assert second is not None and second["id"] == trivy
    assert responder.claim_next(db, _at(1)) is None


def test_the_daily_cap_holds_until_the_next_day(tmp_path):
    db = str(tmp_path / "f.db")
    for n in range(3):
        _in(db, f"HONEYPOT: probe {n}")
    assert responder.claim_next(db, _at(1), cap=2) is not None
    assert responder.claim_next(db, _at(2), cap=2) is not None
    assert responder.claim_next(db, _at(3), cap=2) is None
    assert responder.claim_next(db, _at(24 * 60), cap=2) is not None


def test_a_report_closes_the_incident_only_when_mario_has_nothing_to_do(tmp_path):
    db = str(tmp_path / "f.db")
    fine = _in(db, "Trivy: weekly scan")
    real = _in(db, "HONEYPOT: smb touched")
    assert not responder.save_report(db, fine, REPORT, "s1")  # never handed over
    responder.claim_next(db, _at(1))
    responder.claim_next(db, _at(1))
    assert responder.save_report(db, fine, REPORT, "s1")
    assert not responder.save_report(db, fine, REPORT, "s1")  # only once
    assert responder.save_report(db, real, {**REPORT, "verdict": "needs-mario"}, "s2")
    assert _get(db, fine)["state"] == "handled"
    real_incident = _get(db, real)
    assert real_incident["state"] == "open" and real_incident["session_id"] == "s2"


def test_one_claude_doesnt_get_to_is_missed_sooner_the_more_urgent(tmp_path):
    db = str(tmp_path / "f.db")
    honeypot = _in(db, "HONEYPOT: ssh touched")  # urgent: claim 2, report 8
    trivy = _in(db, "Trivy: weekly scan")  # default: claim 5
    backup = _in(db, "backup full FAILED")  # urgent, reported in time
    assert responder.claim_next(db, _at(1))["id"] == honeypot  # ty: ignore[not-subscriptable]
    assert responder.claim_next(db, _at(1))["id"] == backup  # ty: ignore[not-subscriptable]
    responder.save_report(db, backup, REPORT, "s1")
    assert responder.mark_missed(db, _at(4)) == []
    assert responder.mark_missed(db, _at(5)) == [trivy]  # never claimed
    assert responder.mark_missed(db, _at(9)) == [honeypot]  # claimed, no report
    assert responder.claim_next(db, _at(10)) is None  # missed ones aren't handed later


def test_an_urgent_one_nobody_claims_is_missed_after_two_minutes(tmp_path):
    db = str(tmp_path / "f.db")
    honeypot = _in(db, "HONEYPOT: ssh touched")
    assert responder.mark_missed(db, _at(1)) == []
    assert responder.mark_missed(db, _at(2)) == [honeypot]


def test_a_self_fixing_one_counts_from_when_it_opened(tmp_path):
    db = str(tmp_path / "f.db")
    caddy = _in(db, "clockmost High: Caddy: an upstream is unhealthy")  # high, waits 10 min
    incidents.promote_due(db, _at(11))
    assert responder.mark_missed(db, _at(12)) == []
    assert responder.mark_missed(db, _at(13)) == [caddy]


def test_the_doorbell_rings_only_when_something_waits(tmp_path):
    db = str(tmp_path / "f.db")
    bell = tmp_path / responder.WAKE_FILE
    _in(db, "clockmost: update deployed")  # Felt's, not Claude's
    assert not responder.wake(db) and not bell.exists()
    _in(db, "HONEYPOT: ssh touched")
    assert responder.wake(db) and bell.exists()


def test_her_follow_up_comes_after_her_first_line(tmp_path):
    db = str(tmp_path / "f.db")
    honeypot = _in(db, "HONEYPOT: ssh touched")
    responder.claim_next(db, _at(1))
    assert alert_lines.due(db, _at(1)) == [(honeypot, "line")]  # Claude still looking
    alert_lines.store(db, honeypot, "Someone poked the honeypot, Claude's on it.")
    assert alert_lines.due(db, _at(1)) == []
    responder.save_report(
        db, honeypot, {**REPORT, "verdict": "needs-mario", "for_mario": "check the NAS"}, "s1"
    )
    assert alert_lines.due(db, _at(2)) == [(honeypot, "followup")]
    facts = alert_lines.followup_facts(_get(db, honeypot))
    assert "it needs Mario" in facts and "check the NAS" in facts and "join Claude" in facts
    alert_lines.store(db, honeypot, "It was the NAS, have a look.", "followup")
    assert alert_lines.due(db, _at(3)) == []


def test_the_missed_follow_up_says_claude_couldnt_look(tmp_path):
    db = str(tmp_path / "f.db")
    unclaimed = _in(db, "HONEYPOT: ssh touched")
    alert_lines.store(db, unclaimed, "Honeypot, Claude's on it.")
    responder.mark_missed(db, _at(16))
    facts = alert_lines.followup_facts(_get(db, unclaimed))
    assert "didn't get to it" in facts and "look himself" in facts
    assert alert_lines.due(db, _at(17)) == [(unclaimed, "followup")]


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_API_TOKEN", "main")
    monkeypatch.setenv("FELT_RESPONDER_TOKEN", "resp")
    return TestClient(app)


def test_the_responder_token_opens_its_two_routes_and_nothing_else(tmp_path, monkeypatch):
    resp = {"Authorization": "Bearer resp"}
    with _client(tmp_path, monkeypatch) as c:
        assert c.post("/v1/incidents/handoff").status_code == 401
        assert c.get("/v1/incidents/waiting", headers=resp).json() == {"waiting": 0}
        assert c.post("/v1/incidents/waiting", headers=resp).status_code == 401
        assert c.post("/v1/incidents/handoff", headers=resp).json() == {"incident": None}
        assert c.get("/v1/history", headers=resp).status_code == 401
        assert (
            c.post("/v1/incidents/handoff", headers={"Authorization": "Bearer main"}).status_code
            == 200
        )


def test_handoff_and_report_over_http(tmp_path, monkeypatch):
    resp = {"Authorization": "Bearer resp"}
    with _client(tmp_path, monkeypatch) as c:
        incident_id = _in(str(tmp_path / "felt.db"), "HONEYPOT: ssh touched", at=datetime.now(UTC))
        claimed = c.post("/v1/incidents/handoff", headers=resp).json()["incident"]
        assert (
            claimed["id"] == incident_id
            and claimed["alerts"][0]["title"] == "HONEYPOT: ssh touched"
        )
        body = {"report": {**REPORT, "verdict": "maybe"}, "session_id": "s1"}
        assert (
            c.post(f"/v1/incidents/{incident_id}/report", headers=resp, json=body).status_code
            == 422
        )
        body["report"]["verdict"] = "expected"
        assert c.post(f"/v1/incidents/{incident_id}/report", headers=resp, json=body).json() == {
            "ok": True
        }
        assert (
            c.post(f"/v1/incidents/{incident_id}/report", headers=resp, json=body).status_code
            == 409
        )
        stored = _get(str(tmp_path / "felt.db"), incident_id)
        assert json.loads(stored["report"])["summary"].startswith("The deploy")


def test_a_new_alert_after_claudes_report_is_its_own_incident(tmp_path):
    db = str(tmp_path / "f.db")
    title = "Wazuh L12 [clockmost]: clockmost: sensitive Linux file changed: /etc/a"
    first = _in(db, title)
    during = _in(db, title.replace("/etc/a", "/etc/b"), at=_at(1))
    assert during == first  # still being looked at: same problem
    responder.claim_next(db, _at(1))
    assert _in(db, title.replace("/etc/a", "/etc/c"), at=_at(2)) == first  # he sees it live
    responder.save_report(db, first, REPORT, "s1")
    after = _in(db, title.replace("/etc/a", "/etc/shadow"), at=_at(5))
    assert after != first and _get(db, after)["state"] == "open"


def test_when_claude_beats_her_first_line_she_says_both_in_one(tmp_path):
    db = str(tmp_path / "f.db")
    trivy = _in(db, "Trivy: weekly scan")
    responder.claim_next(db, _at(0))
    responder.save_report(db, trivy, REPORT, "s1")  # expected -> handled, no line yet
    assert alert_lines.due(db, _at(1)) == [(trivy, "both")]
    alert_lines.store(db, trivy, "The weekly scan ran; Claude says it's our own deploy.", "both")
    incident = _get(db, trivy)
    assert incident["line"] == incident["followup"]
    assert alert_lines.due(db, _at(2)) == []


def test_one_mario_closed_himself_gets_no_line(tmp_path):
    db = str(tmp_path / "f.db")
    from felt_core import systems

    honeypot = _in(db, "HONEYPOT: ssh touched")
    responder.claim_next(db, _at(0))
    responder.save_report(db, honeypot, {**REPORT, "verdict": "needs-mario"}, "s1")
    systems.mark_handled(db, honeypot)
    assert alert_lines.due(db, _at(1)) == []
