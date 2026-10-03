"""WP-44 step 2: her line about an incident - the facts are fixed (catalogue
+ incident), her 12B only words them; lines live with the incident and in
her log as kind alert, never in the Margins."""

import asyncio
from datetime import UTC, datetime, timedelta

from felt_core import alert_lines, incidents, margins
from felt_core.alert_types import CATALOGUE
from felt_core.db import ALERT, _connect

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _at(minutes):
    return T0 + timedelta(minutes=minutes)


def _incident(db, title, message="", *, at):
    incident_id = incidents.intake(db, title, message, at=at)
    assert incident_id is not None
    incident = incidents.get(db, incident_id)
    assert incident is not None
    return incident


def test_every_catalogue_type_has_plain_words():
    assert {t.name for t in CATALOGUE} | {"unknown"} <= set(alert_lines.MEANINGS)


def test_facts_carry_expected_todo_and_whos_on_it(tmp_path):
    db = str(tmp_path / "f.db")
    update = alert_lines.facts(
        _incident(db, "clockmost: update deployed", "immich — verified + healthy", at=_at(0))
    )
    assert "Expected: yes" in update and "to do: nothing" in update
    assert "immich — verified + healthy" in update
    disk = alert_lines.facts(_incident(db, "clockmost: storage/backup check", at=_at(0)))
    assert "Claude is looking into it" in disk and "plugged in" in disk
    caddy = _incident(db, "clockmost High: Caddy: an upstream is unhealthy", at=_at(0))
    incidents.promote_due(db, _at(11))
    caddy = incidents.get(db, caddy["id"])
    assert caddy is not None
    assert "didn't fix itself within 10 minutes" in alert_lines.facts(caddy)


def test_which_incidents_are_owed_a_line(tmp_path):
    db = str(tmp_path / "f.db")
    backup = _incident(db, "backup full FAILED", at=_at(0))["id"]  # open at once
    caddy = _incident(db, "clockmost High: Caddy: an upstream is unhealthy", at=_at(0))["id"]
    update = _incident(db, "clockmost: update deployed", at=_at(0))["id"]
    _incident(db, 'clockmost Average: Windows: "AppXSvc" is not running', at=_at(0))  # dropped
    # pending caddy isn't owed one; the update run hasn't settled yet
    assert alert_lines.due(db, _at(5)) == [(backup, False)]
    assert alert_lines.due(db, _at(15)) == [(backup, False), (update, False)]
    incidents.promote_due(db, _at(11))
    assert (caddy, False) in alert_lines.due(db, _at(15))
    alert_lines.store(db, caddy, "Caddy's sulking.", back=False)
    incidents.intake(db, "clockmost RESOLVED: Caddy: an upstream is unhealthy", at=_at(30))
    assert (caddy, True) in alert_lines.due(db, _at(31))
    alert_lines.store(db, caddy, "Caddy's back.", back=True)
    assert all(i != caddy for i, _ in alert_lines.due(db, _at(32)))


def test_lines_go_to_her_log_as_alerts_not_the_margins(tmp_path):
    db = str(tmp_path / "f.db")
    backup = _incident(db, "backup full FAILED", at=_at(0))["id"]
    alert_lines.store(db, backup, "Backup failed, Claude's on it.", back=False)
    conn = _connect(db)
    rows = conn.execute("SELECT kind, source_ref, content FROM messages").fetchall()
    conn.close()
    assert rows == [(ALERT, f"incident:{backup}", "Backup failed, Claude's on it.")]
    assert margins.page(db) == [] and margins.unread(db) == 0
    stored = incidents.get(db, backup)
    assert stored is not None and stored["line"] == "Backup failed, Claude's on it."


def test_write_due_words_each_owed_line_and_stops_when_the_model_is_down(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    _incident(db, "backup full FAILED", at=datetime.now(UTC))
    _incident(db, "clockmost: storage/backup check", at=datetime.now(UTC))

    class Config:
        db_path = db

    said = []

    async def fake_line(config, incident, back=False):
        said.append(incident["type"])
        return f"line about {incident['type']}"

    monkeypatch.setattr(alert_lines, "line_for", fake_line)
    assert asyncio.run(alert_lines.write_due(Config())) == 2  # ty: ignore[invalid-argument-type]
    assert said == ["backup-failed", "storage-check"]
    assert asyncio.run(alert_lines.write_due(Config())) == 0  # ty: ignore[invalid-argument-type]

    from felt_core import local_llm

    async def down(config, incident, back=False):
        raise local_llm.LocalLLMUnavailable("down")

    _incident(db, "HONEYPOT: x", at=datetime.now(UTC))
    monkeypatch.setattr(alert_lines, "line_for", down)
    assert asyncio.run(alert_lines.write_due(Config())) == 0  # ty: ignore[invalid-argument-type]


def test_long_replies_are_clipped():
    assert len(alert_lines._clip(" ".join(["word"] * 80)).split()) == alert_lines.MAX_WORDS


def test_after_her_line_the_next_update_is_its_own_news(tmp_path):
    db = str(tmp_path / "f.db")
    first = _incident(db, "clockmost: update deployed", "immich", at=_at(0))["id"]
    assert _incident(db, "clockmost: update deployed", "caddy", at=_at(5))["id"] == first
    alert_lines.store(db, first, "Updated immich and caddy.", back=False)
    assert _incident(db, "clockmost: update deployed", "felt-core", at=_at(20))["id"] != first
