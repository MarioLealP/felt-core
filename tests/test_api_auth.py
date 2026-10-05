"""WP-55: optional bearer-token auth on the API."""

from fastapi.testclient import TestClient

from felt_core.api import app


def _client(tmp_path, monkeypatch, token=None):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    if token:
        monkeypatch.setenv("FELT_API_TOKEN", token)
    else:
        monkeypatch.delenv("FELT_API_TOKEN", raising=False)
    return TestClient(app)


def test_no_token_configured_means_open(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get("/v1/history").status_code == 200


def test_token_required_on_data_routes(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, "s3cret") as c:
        r = c.get("/v1/history")
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        assert "access-control-allow-origin" not in r.headers
        assert c.get("/v1/history", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/v1/history", headers={"Authorization": "s3cret"}).status_code == 401
        assert c.get("/v1/history", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_healthcheck_docs_and_preflight_stay_open(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, "s3cret") as c:
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/docs").status_code == 200
        r = c.options(
            "/v1/history",
            headers={"Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "GET"},
        )
        assert r.status_code != 401  # no credentials needed to be told no
        assert "access-control-allow-origin" not in r.headers


def test_no_cors_for_foreign_origins(tmp_path, monkeypatch):
    """WP-62 0.2: no wildcard (or any) CORS - a foreign page can't read
    felt-core's answers, with or without credentials."""
    with _client(tmp_path, monkeypatch, "s3cret") as c:
        for origin in ("https://evil.example", "null", "https://felt.clockmost.com"):
            r = c.get("/v1/history", headers={"Origin": origin, "Authorization": "Bearer s3cret"})
            assert r.status_code == 200
            assert not any(h.startswith("access-control-") for h in r.headers)


def test_websocket_needs_the_token_too(tmp_path, monkeypatch):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with _client(tmp_path, monkeypatch, "s3cret") as c:
        with pytest.raises(WebSocketDisconnect) as e, c.websocket_connect("/v1/chat/ws"):
            pass
        assert e.value.code == 1008
        with c.websocket_connect("/v1/chat/ws?token=s3cret"):
            pass
        with c.websocket_connect("/v1/chat/ws", headers={"Authorization": "Bearer s3cret"}):
            pass


def test_shopping_list_paths_are_left_to_caddy_sso(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, "s3cret") as c:
        assert c.get("/shopping-list").status_code == 200
        assert c.get("/v1/shopping-list").status_code == 200
        item = c.post("/v1/shopping-list", json={"text": "milk"}).json()["id"]
        assert c.patch(f"/v1/shopping-list/{item}?checked=true").status_code == 200
        assert c.delete("/v1/shopping-list/checked").status_code == 200
        assert c.delete(f"/v1/shopping-list/{item}").status_code == 200
        # exact paths only: no prefix match, no other methods
        assert c.get("/v1/shopping-list/1").status_code == 401
        assert c.get("/v1/shopping-listx").status_code == 401
        assert c.put("/v1/shopping-list/1").status_code == 401
