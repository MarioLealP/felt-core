"""WP-62 M2b B5.4 (D3): Felt machine identities from Caddy's machine host -
per identity per route, deny by default, serial deny list."""

from fastapi.testclient import TestClient

from felt_core.api import app
from felt_core.machine_auth import MACHINE_ROUTES, authorize

W = "spiffe://clockmost.internal/workload/"


def _client(tmp_path, monkeypatch, token="s3cret", deny=None, on=True):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_MACHINE_IDENTITY", "true" if on else "false")
    if token:
        monkeypatch.setenv("FELT_API_TOKEN", token)
    else:
        monkeypatch.delenv("FELT_API_TOKEN", raising=False)
    if deny is not None:
        monkeypatch.setenv("FELT_MACHINE_DENY_SERIALS", deny)
    else:
        monkeypatch.delenv("FELT_MACHINE_DENY_SERIALS", raising=False)
    return TestClient(app)


def _as(identity, serial="1"):
    return {"X-Felt-Identity": W + identity, "X-Felt-Serial": serial}


def test_tray_gets_its_route_without_the_token(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get("/v1/settings/temperature-unit", headers=_as("tray")).status_code == 200


def test_tray_refused_off_its_list(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        r = c.get("/v1/history", headers=_as("tray"))
        assert r.status_code == 403
        assert r.json()["detail"] == "route not allowed for this identity"
        # a valid identity header never upgrades to the token's full access
        assert (
            c.get(
                "/v1/history", headers=_as("tray") | {"Authorization": "Bearer s3cret"}
            ).status_code
            == 403
        )


def test_identities_are_separate(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get("/v1/notices", headers=_as("felt-animation")).status_code == 403
        assert c.post("/v1/text-chat", json={}, headers=_as("felt-responder")).status_code == 403
        assert c.get("/v1/incidents/waiting", headers=_as("felt-responder")).status_code != 403


def test_unknown_identity_refused(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        r = c.get("/v1/settings/temperature-unit", headers=_as("felt-core"))
        assert r.status_code == 403 and r.json()["detail"] == "unknown identity"


def test_denied_serial_refused_at_once(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, deny='["777"]') as c:
        r = c.get("/v1/settings/temperature-unit", headers=_as("tray", "777"))
        assert r.status_code == 403 and r.json()["detail"] == "revoked certificate"
        assert c.get("/v1/settings/temperature-unit", headers=_as("tray", "778")).status_code == 200


def test_identity_enforced_even_without_a_token(tmp_path, monkeypatch):
    """No FELT_API_TOKEN used to mean 'open'; a machine identity is still checked."""
    with _client(tmp_path, monkeypatch, token=None) as c:
        assert c.get("/v1/history", headers=_as("tray")).status_code == 403


def test_without_the_header_nothing_changes(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get("/v1/history").status_code == 401
        assert c.get("/v1/history", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_route_table_covers_each_client():
    assert set(MACHINE_ROUTES) == {W + "tray", W + "felt-animation", W + "felt-responder"}
    assert authorize(W + "tray", "", "GET", "/v1/reply/abc_1/chunk/3", []) is None
    assert authorize(W + "tray", "", "GET", "/v1/reply/../chunk/3", []) is not None
    assert authorize(W + "felt-animation", "", "POST", "/v1/reaction-tag", []) is None
    assert authorize(W + "felt-responder", "", "POST", "/v1/incidents/12/report", []) is None
    assert authorize(W + "felt-responder", "", "POST", "/v1/incidents/12/handled", []) is not None


def test_header_ignored_until_switched_on(tmp_path, monkeypatch):
    """Off by default: until Caddy strips the header everywhere else, a claimed
    identity counts for nothing (the token is still required)."""
    with _client(tmp_path, monkeypatch, on=False) as c:
        assert c.get("/v1/settings/temperature-unit", headers=_as("tray")).status_code == 401
