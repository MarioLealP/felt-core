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


def test_sso_mutations_reject_a_foreign_origin(tmp_path, monkeypatch):
    """WP-62 phase 4: the SSO-gated routes carry no token (Caddy forward-auth
    is the auth), the forward-auth cookie is SameSite=Lax and every
    *.clockmost.com app is same-site - so felt-core refuses an unsafe SSO
    request whose Origin isn't the app's own, to stop a sibling-subdomain CSRF.
    Native clients use the token path (not these routes) and send no Origin."""
    with _client(tmp_path, monkeypatch, "s3cret") as c:
        app_origin = "https://felt.clockmost.com"
        # same-origin (and native-ish no-Origin) mutations still work
        assert c.post("/v1/shopping-list", json={"text": "milk"}).status_code == 200
        assert (
            c.post(
                "/v1/shopping-list",
                json={"text": "eggs"},
                headers={"Origin": app_origin, "Sec-Fetch-Site": "same-origin"},
            ).status_code
            == 200
        )
        # felt-tray's window on the cert-only host is the same app (2026-10-17)
        assert (
            c.post(
                "/v1/shopping-list",
                json={"text": "tea"},
                headers={
                    "Origin": "https://felt-desktop.clockmost.com",
                    "Sec-Fetch-Site": "same-origin",
                },
            ).status_code
            == 200
        )
        # a foreign origin - a sibling subdomain or anywhere - is refused
        for origin in ("https://calc.clockmost.com", "https://evil.example"):
            assert (
                c.post(
                    "/v1/shopping-list", json={"text": "x"}, headers={"Origin": origin}
                ).status_code
                == 403
            ), origin
        # cross-site fetch metadata alone (no Origin header) is refused too
        assert (
            c.post(
                "/v1/shopping-list", json={"text": "x"}, headers={"Sec-Fetch-Site": "cross-site"}
            ).status_code
            == 403
        )
        # a safe cross-origin read is unaffected (no state change)
        assert (
            c.get("/v1/shopping-list", headers={"Origin": "https://calc.clockmost.com"}).status_code
            == 200
        )
