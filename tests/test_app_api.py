"""WP-42.2: the Felt app's SSO-gated API."""

from fastapi.testclient import TestClient

import felt_core.app_api as app_api
from felt_core.api import app
from felt_core.app_history import history_page
from felt_core.db import save_message

SSO = {"X-Authentik-Username": "mario"}


def _client(tmp_path, monkeypatch, token="s3cret"):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_API_TOKEN", token)
    return TestClient(app)


def test_app_routes_skip_the_token_but_need_the_sso_header(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get("/v1/app/me").status_code == 401
        r = c.get("/v1/app/me", headers=SSO)
        assert r.status_code == 200
        assert r.json() == {"user": "mario"}
        assert c.get("/v1/app/history").status_code == 401


def test_other_routes_still_need_the_token(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get("/v1/history", headers=SSO).status_code == 401
        assert c.post("/v1/app/nope", headers=SSO).status_code == 401


def test_history_pages_backwards_with_ids_and_times(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        db = str(tmp_path / "felt.db")
        for i in range(5):
            save_message(db, "dashboard", "user", f"line {i}")
        save_message(db, "other", "user", "elsewhere")
        page = c.get("/v1/app/history?limit=3", headers=SSO).json()
        assert [m["content"] for m in page] == ["line 2", "line 3", "line 4"]
        assert page[0]["created_at"].endswith("Z") and "T" in page[0]["created_at"]
        older = c.get(f"/v1/app/history?limit=3&before={page[0]['id']}", headers=SSO).json()
        assert [m["content"] for m in older] == ["line 0", "line 1"]


def test_chat_returns_the_stored_pair(tmp_path, monkeypatch):
    async def fake_chat(text, config, session_id):
        save_message(config.db_path, session_id, "user", text)
        save_message(config.db_path, session_id, "assistant", "Done. On the list.")

    monkeypatch.setattr(app_api, "chat", fake_chat)
    with _client(tmp_path, monkeypatch) as c:
        r = c.post("/v1/app/chat", json={"message": " add oat milk "}, headers=SSO)
        assert r.status_code == 200
        msgs = r.json()["messages"]
        assert [(m["role"], m["content"]) for m in msgs] == [
            ("user", "add oat milk"),
            ("assistant", "Done. On the list."),
        ]
        assert c.post("/v1/app/chat", json={"message": "  "}, headers=SSO).status_code == 422


def test_history_page_clamps_the_limit(tmp_path):
    db = str(tmp_path / "felt.db")
    save_message(db, "dashboard", "user", "hi")
    assert len(history_page(db, "dashboard", limit=0)) == 1
    assert len(history_page(db, "dashboard", limit=10_000)) == 1
