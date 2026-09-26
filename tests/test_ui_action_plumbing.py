"""End-to-end check that a skill's UIAction actually reaches the client -
this plumbing (SkillResult.ui_action -> ChatResponse.ui_action /
X-UI-Action header) existed before WP-09 but had no test covering it start
to finish. felt_presence (WP-09's "shut up"/"you can talk again") is the
first skill to actually use it, so it's a real, concrete case to verify
against rather than a synthetic one."""

import json
from urllib.parse import unquote

from fastapi.testclient import TestClient

import felt_core.api as api
import felt_core.brain.orchestrator as orchestrator
import felt_core.brain.replies as brain


def _no_cloud(monkeypatch):
    # Forces _phrase_skill_result's local fallback path (no real network
    # call) - felt_presence is "public" (skills.json), which would
    # otherwise try cloud_generate first. FeltConfig is a pydantic-settings
    # BaseSettings with env_prefix="FELT_", so this is FELT_GEMINI_API_KEY,
    # not the bare GEMINI_API_KEY name some other tooling uses.
    monkeypatch.delenv("FELT_GEMINI_API_KEY", raising=False)

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        return "okay okay, I'll zip it"

    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)


def test_v1_chat_carries_felt_presence_ui_action(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    _no_cloud(monkeypatch)

    with TestClient(api.app) as client:
        response = client.post("/v1/chat", json={"message": "shut up", "session_id": "wp09-test"})

    assert response.status_code == 200
    body = response.json()
    assert body["ui_action"] == {"kind": "presence", "payload": {"action": "hush", "minutes": 10}}


def test_v1_chat_unhush_phrasing(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    _no_cloud(monkeypatch)

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat", json={"message": "you can talk again", "session_id": "wp09-test"}
        )

    assert response.status_code == 200
    assert response.json()["ui_action"] == {"kind": "presence", "payload": {"action": "unhush"}}


def test_v1_text_chat_puts_ui_action_in_the_header(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_VOICE_URL", "http://fake-voice")
    _no_cloud(monkeypatch)
    monkeypatch.setattr(api.tts_cache, "get", lambda text: b"fake-wav-bytes")

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/text-chat", json={"message": "shut up", "session_id": "wp09-test"}
        )

    assert response.status_code == 200
    raw = response.headers.get("x-ui-action")
    assert raw is not None
    assert json.loads(unquote(raw)) == {
        "kind": "presence",
        "payload": {"action": "hush", "minutes": 10},
    }


def test_v1_chat_plain_message_has_no_ui_action(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    _no_cloud(monkeypatch)

    async def fake_decide_tool(message, registry, cfg):
        return None

    monkeypatch.setattr(orchestrator, "decide_tool", fake_decide_tool)

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat", json={"message": "hello there", "session_id": "wp09-test"}
        )

    assert response.status_code == 200
    assert response.json()["ui_action"] is None
