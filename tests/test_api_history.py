from fastapi.testclient import TestClient

from felt_core.api import app
from felt_core.db import save_message


def test_history_endpoint_returns_saved_messages(tmp_path, monkeypatch):
    db_path = str(tmp_path / "felt.db")
    monkeypatch.setenv("FELT_DB_PATH", db_path)
    save_message(db_path, "default", "user", "hi")
    save_message(db_path, "default", "assistant", "hello")

    with TestClient(app) as client:
        response = client.get("/v1/history")

    assert response.status_code == 200
    assert response.json() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_history_endpoint_is_scoped_per_session(tmp_path, monkeypatch):
    db_path = str(tmp_path / "felt.db")
    monkeypatch.setenv("FELT_DB_PATH", db_path)
    save_message(db_path, "default", "user", "for default")
    save_message(db_path, "other", "user", "for other")

    with TestClient(app) as client:
        response = client.get("/v1/history", params={"session_id": "other"})

    assert response.json() == [{"role": "user", "content": "for other"}]


def test_history_endpoint_empty_session_returns_empty_list(tmp_path, monkeypatch):
    db_path = str(tmp_path / "felt.db")
    monkeypatch.setenv("FELT_DB_PATH", db_path)

    with TestClient(app) as client:
        response = client.get("/v1/history")

    assert response.json() == []
