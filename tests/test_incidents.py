"""WP-44 step 1: alerts become incidents - typed from the catalogue, folded,
paired with their RESOLVED, and self-fixing ones wait before they count."""

from datetime import UTC, datetime, timedelta

from felt_core import incidents
from felt_core.alert_types import CATALOGUE, parse

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _at(minutes):
    return T0 + timedelta(minutes=minutes)


def _db(tmp_path):
    return str(tmp_path / "f.db")


def _in(db, title, message="", *, at):
    incident_id = incidents.intake(db, title, message, at=at)
    assert incident_id is not None
    return incident_id


def _get(db, incident_id):
    incident = incidents.get(db, incident_id)
    assert incident is not None
    return incident


def test_real_titles_get_their_catalogue_type():
    cases = [
        ("clockmost High: Caddy: an upstream is unhealthy", "caddy-upstream"),
        ("clockmost RESOLVED: Caddy: an upstream is unhealthy", "caddy-upstream"),
        ("backup full FAILED", "backup-failed"),
        ("clockmost: updates SKIPPED", "updates-skipped"),
        ("clockmost: update deployed", "update-deployed"),
        ("Wazuh L12 [clockmost]: sensitive Linux file changed: /etc/x", "wazuh-fim"),
        ("Wazuh L12 [clockmost]: System running out of memory.", "wazuh-oom"),
        ("HONEYPOT: someone banned", "honeypot"),
        ("clockmost Warning: Linux: clockmost-nitro5 has been restarted", "host-restarted"),
        ("clockmost High: Docker: Container /suricata: Health state unhealthy", "container-down"),
        ("clockmost Average: Windows: Zabbix agent is not available", "pc-agent"),
        ("clockmost Average: Linux: Zabbix agent is not available", "zabbix-agent"),
        ("clockmost Warning: Linux: Number of installed packages changed", "packages-changed"),
        ("clockmost Average: clockmost-nitro5: Ollama service FAILED", "nitro-ollama"),
        ("something nobody has seen before", "unknown"),
    ]
    for title, name in cases:
        assert parse(title).type.name == name, title
    assert parse("clockmost RESOLVED: Caddy: an upstream is unhealthy").resolves
    # severity isn't part of the key: Warning then High is the same problem
    assert parse("clockmost Warning: X").key == parse("clockmost High: X").key == "X"


def test_catalogue_names_are_unique():
    names = [t.name for t in CATALOGUE]
    assert len(names) == len(set(names))


def test_a_self_fixing_outage_that_fixes_itself_is_cleared(tmp_path):
    db = _db(tmp_path)
    i = _in(db, "clockmost High: Caddy: an upstream is unhealthy", at=_at(0))
    assert _get(db, i)["state"] == "pending"
    assert _in(db, "clockmost RESOLVED: Caddy: an upstream is unhealthy", at=_at(4)) == i
    assert _get(db, i)["state"] == "cleared"
    assert incidents.promote_due(db, _at(30)) == []


def test_one_that_outlasts_its_wait_becomes_open(tmp_path):
    db = _db(tmp_path)
    i = _in(db, "clockmost High: Caddy: an upstream is unhealthy", at=_at(0))
    assert incidents.promote_due(db, _at(9)) == []
    assert incidents.promote_due(db, _at(11)) == [i]
    assert _get(db, i)["state"] == "open"
    _in(db, "clockmost RESOLVED: Caddy: an upstream is unhealthy", at=_at(40))
    got = _get(db, i)
    assert got["state"] == "resolved" and got["resolved_at"] == "2026-10-03 12:40:00"


def test_repeats_fold_and_a_flapping_check_waits_again(tmp_path):
    db = _db(tmp_path)
    title = "clockmost High: Caddy: an upstream is unhealthy"
    i = _in(db, title, at=_at(0))
    _in(db, "clockmost RESOLVED: Caddy: an upstream is unhealthy", at=_at(3))
    assert _in(db, title, at=_at(60)) == i  # same problem, folded
    got = _get(db, i)
    assert got["count"] == 2 and got["state"] == "pending"
    assert incidents.promote_due(db, _at(65)) == []  # a fresh wait from 60
    assert incidents.promote_due(db, _at(71)) == [i]


def test_folding_stops_after_the_window(tmp_path):
    db = _db(tmp_path)
    a = _in(db, "backup full FAILED", at=_at(0))
    assert _in(db, "backup full FAILED", at=_at(60)) == a
    b = _in(db, "backup full FAILED", at=_at(60 * 8))
    assert b != a and _get(db, a)["count"] == 2


def test_updates_skipped_joins_the_failed_backup(tmp_path):
    db = _db(tmp_path)
    a = _in(db, "backup full FAILED", "restic exit 10", at=_at(0))
    assert _in(db, "clockmost: updates SKIPPED", at=_at(0.1)) == a
    assert [x["title"] for x in _get(db, a)["alerts"]] == [
        "backup full FAILED",
        "clockmost: updates SKIPPED",
    ]


def test_expected_events_are_noted_and_an_update_run_is_one_incident(tmp_path):
    db = _db(tmp_path)
    a = _in(db, "clockmost: update deployed", "immich", at=_at(0))
    assert _in(db, "clockmost: update deployed", "caddy", at=_at(2)) == a
    got = _get(db, a)
    assert got["state"] == "noted" and got["expected"] and got["count"] == 2
    # the next night's run is its own line
    assert _in(db, "clockmost: update deployed", at=_at(60 * 24)) != a


def test_security_goes_to_claude_and_noise_is_dropped(tmp_path):
    db = _db(tmp_path)
    w = _get(db, _in(db, "Wazuh L12 [clockmost]: file changed", at=_at(0)))
    assert (w["state"], w["handler"], w["tier"]) == ("open", "claude", "high")
    n = _get(
        db,
        _in(db, 'clockmost Average: Windows: "AppXSvc" (AppX) is not running', at=_at(0)),
    )
    assert n["state"] == "dropped"


def test_a_resolved_for_something_we_never_saw_is_ignored(tmp_path):
    db = _db(tmp_path)
    assert incidents.intake(db, "clockmost RESOLVED: Pi-hole: DNS not resolving", at=_at(0)) is None


def test_a_deploy_touching_several_files_is_one_wazuh_incident(tmp_path):
    db = _db(tmp_path)
    a = _in(db, "Wazuh L12 [clockmost]: sensitive Linux file changed: /etc/a", at=_at(0))
    assert _in(db, "Wazuh L12 [clockmost]: sensitive Linux file changed: /etc/b", at=_at(1)) == a
    other_host = _in(db, "Wazuh L12 [clockmost-desktop]: autostart changed", at=_at(1))
    assert other_host != a
    assert len(_get(db, a)["alerts"]) == 2  # Claude still sees every file


def test_an_update_revert_is_one_incident(tmp_path):
    db = _db(tmp_path)
    a = _in(db, "clockmost: auto-reverting an update", at=_at(0))
    assert _in(db, "clockmost: update reverted", at=_at(3)) == a
