"""WP-42.3: the Margins page - her non-chat lines with read / dismissed /
snoozed state, behind SSO."""

import sqlite3

from fastapi.testclient import TestClient

from felt_core import margins
from felt_core.api import app
from felt_core.asides import current_asides
from felt_core.db import save_message

SSO = {"X-Authentik-Username": "mario"}


def _lines(db: str) -> dict[str, int]:
    """A conversation with one of each kind; returns content -> id."""
    rows = [
        ("user", "hi", "chat", None),
        ("assistant", "Hey.", "chat", None),
        ("assistant", "Spreadsheets again?", "remark", "screen:3"),
        ("assistant", "Good song.", "event", "event:song"),
        ("assistant", "Dentist in 15.", "proactive", "proactive:calendar_15:1"),
        ("assistant", "Alarm: stretch", "alarm", "alarm:2"),
    ]
    ids = {c: save_message(db, "dashboard", r, c, kind=k, source_ref=s) for r, c, k, s in rows}
    save_message(db, "other", "assistant", "elsewhere", kind="remark")
    return ids


def test_the_page_is_her_non_chat_lines_with_a_source(tmp_path):
    db = str(tmp_path / "felt.db")
    _lines(db)
    got = margins.page(db)
    assert [(m["content"], m["source"]) for m in got] == [
        ("Spreadsheets again?", "remark"),
        ("Good song.", "song"),
        ("Dentist in 15.", "calendar_15"),
        ("Alarm: stretch", "alarm"),
    ]
    assert got[0]["created_at"].endswith("Z") and got[0]["read"] is False
    assert margins.unread(db) == 4
    older = margins.page(db, before_id=got[2]["id"], limit=1)
    assert [m["content"] for m in older] == ["Good song."]


def test_read_dismiss_and_snooze(tmp_path):
    db = str(tmp_path / "felt.db")
    ids = _lines(db)
    # chat lines and other sessions can't be touched
    assert margins.mark(db, [ids["hi"], ids["Hey."]], "read") == 0
    assert margins.mark(db, [ids["Spreadsheets again?"]], "read") == 1
    assert margins.unread(db) == 3
    assert margins.mark(db, [ids["Good song."]], "dismiss") == 1
    assert margins.mark(db, [ids["Dentist in 15."]], "snooze", hours=2) == 1
    assert [m["content"] for m in margins.page(db)] == ["Spreadsheets again?", "Alarm: stretch"]
    assert margins.unread(db) == 1
    assert margins.mark(db, [ids["Alarm: stretch"]], "explode") == 0
    # a snooze runs out: back, and unread again
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE messages SET snoozed_until = datetime('now', '-1 minute')")
    conn.close()
    back = [m for m in margins.page(db) if m["content"] == "Dentist in 15."]
    assert back and back[0]["read"] is False


def test_read_all_leaves_snoozed_lines_and_other_sessions(tmp_path):
    db = str(tmp_path / "felt.db")
    ids = _lines(db)
    assert margins.mark(db, [ids["Dentist in 15."]], "snooze", hours=2) == 1
    assert margins.read_all(db) == 3
    assert margins.unread(db) == 0
    assert margins.unread(db, "other") == 1
    # the snoozed one comes back unread, to be noticed again
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE messages SET snoozed_until = datetime('now', '-1 minute')")
    conn.close()
    assert margins.unread(db) == 1


def test_a_dismissed_line_stops_waiting_as_an_aside(tmp_path):
    db = str(tmp_path / "felt.db")
    ids = _lines(db)
    before = [a["content"] for a in current_asides(db)["asides"]]
    assert "Dentist in 15." in before
    margins.mark(db, [ids["Dentist in 15."]], "dismiss")
    assert "Dentist in 15." not in [a["content"] for a in current_asides(db)["asides"]]


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_API_TOKEN", "s3cret")
    return TestClient(app)


def test_routes_are_sso_gated_and_work(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        ids = _lines(str(tmp_path / "felt.db"))
        assert c.get("/v1/app/margins").status_code == 401
        assert c.get("/v1/app/margins/count").status_code == 401
        assert c.post("/v1/app/margins/mark", json={}).status_code == 401
        assert len(c.get("/v1/app/margins", headers=SSO).json()) == 4
        assert c.get("/v1/app/margins/count", headers=SSO).json() == {"unread": 4}
        r = c.post(
            "/v1/app/margins/mark",
            json={"ids": [ids["Good song."], ids["Alarm: stretch"]], "action": "read"},
            headers=SSO,
        )
        assert r.json() == {"changed": 2}
        assert c.get("/v1/app/margins/count", headers=SSO).json() == {"unread": 2}
        bad = {"ids": [1], "action": "explode"}
        assert c.post("/v1/app/margins/mark", json=bad, headers=SSO).status_code == 422
        empty = {"ids": [], "action": "read"}
        assert c.post("/v1/app/margins/mark", json=empty, headers=SSO).status_code == 422
        # "mark all read": only as read, never mixed with ids
        for bad in (
            {"all": True, "action": "dismiss"},
            {"all": True, "ids": [1], "action": "read"},
        ):
            assert c.post("/v1/app/margins/mark", json=bad, headers=SSO).status_code == 422
        r = c.post("/v1/app/margins/mark", json={"all": True, "action": "read"}, headers=SSO)
        assert r.json() == {"changed": 2}
        assert c.get("/v1/app/margins/count", headers=SSO).json() == {"unread": 0}
