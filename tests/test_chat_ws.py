"""WP-40: /v1/chat/ws used to swallow any error with a bare `except
Exception: await websocket.close()` and zero logging - the single worst
offender across ~50+ broad excepts audited (ENGINEERING-RULES.md rule
33/34). Now logs what was happening before closing the socket, and treats
an ordinary client disconnect as expected rather than an error."""

import contextlib
import logging

from fastapi.testclient import TestClient

import felt_core.api as api
from felt_core.api import app
from felt_core.local_llm import LocalLLMUnavailable


def test_chat_ws_happy_path_still_works(tmp_path, monkeypatch, fake_llm):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))

    fake_llm.default = "hi there"

    with TestClient(app) as client, client.websocket_connect("/v1/chat/ws") as ws:
        ws.send_json({"message": "hello", "session_id": "wp40-ws-test"})
        reply = ws.receive_json()

    assert reply["reply"] == "hi there"


def test_chat_ws_reports_felt_llm_offline_and_stays_open(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))

    async def offline_chat(message, config, session_id="default", tone=None):
        raise LocalLLMUnavailable("down")

    monkeypatch.setattr(api, "chat", offline_chat)

    with TestClient(app) as client, client.websocket_connect("/v1/chat/ws") as ws:
        ws.send_json({"message": "hello"})
        assert ws.receive_json() == {"error": "felt-llm offline"}
        ws.send_json({"message": "still there?"})
        assert ws.receive_json() == {"error": "felt-llm offline"}


def test_chat_ws_logs_and_closes_on_an_unexpected_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))

    async def broken_chat(message, config, session_id="default", tone=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "chat", broken_chat)

    with (
        caplog.at_level(logging.ERROR, logger="felt_core.api"),
        TestClient(app) as client,
        client.websocket_connect("/v1/chat/ws") as ws,
    ):
        ws.send_json({"message": "hello", "session_id": "wp40-ws-test"})
        # The server closes the socket after logging the failure -
        # receiving anything further should surface that closure, not
        # hang.
        with contextlib.suppress(Exception):
            ws.receive_json()

    assert any("chat_ws" in record.message for record in caplog.records)


def test_chat_ws_client_disconnect_is_logged_at_info_not_error(
    tmp_path, monkeypatch, caplog, fake_llm
):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))

    fake_llm.default = "hi there"

    with (
        caplog.at_level(logging.INFO, logger="felt_core.api"),
        TestClient(app) as client,
        client.websocket_connect("/v1/chat/ws") as ws,
    ):
        ws.send_json({"message": "hello", "session_id": "wp40-ws-test"})
        ws.receive_json()
        # Closing the client side is what makes receive_json() raise
        # WebSocketDisconnect server-side on its next receive.

    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
