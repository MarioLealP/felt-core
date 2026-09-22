import felt_core.brain as brain
from felt_core.config import FeltConfig
from felt_core.db import get_history


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


async def test_cloud_path_receives_no_chat_history(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")
    captured = {}

    async def fake_cloud_chat(message, history, cfg):
        captured["history"] = history
        return "nice reaction"

    async def fake_is_safe_for_cloud(text, cfg):
        return True

    monkeypatch.setattr(brain, "_cloud_chat", fake_cloud_chat)
    monkeypatch.setattr(brain, "is_safe_for_cloud", fake_is_safe_for_cloud)

    result = await brain.process_screen_context("a cat on the desk", config, session_id="wp02-test")

    assert result.reply == "nice reaction"
    assert captured["history"] == []


async def test_none_reply_triggers_exactly_one_local_retry(tmp_path, monkeypatch):
    config = _config(tmp_path)  # no gemini key -> straight to the local path
    calls = []

    async def fake_ollama_chat(messages, model, cfg):
        calls.append(messages)
        return "NONE" if len(calls) == 1 else "okay something real"

    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    result = await brain.process_screen_context("something", config, session_id="wp02-test")

    assert result.reply == "okay something real"
    assert len(calls) == 2


async def test_still_empty_after_retry_returns_silent_and_stops(tmp_path, monkeypatch):
    config = _config(tmp_path)
    calls = []

    async def fake_ollama_chat(messages, model, cfg):
        calls.append(messages)
        return "NONE"

    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    result = await brain.process_screen_context("something", config, session_id="wp02-test")

    assert result.reply == ""
    assert len(calls) == 2  # first attempt + exactly one retry, no more


async def test_second_call_inside_cooldown_makes_zero_model_calls(tmp_path, monkeypatch):
    config = _config(tmp_path, screen_reaction_cooldown_s=90)
    calls = []

    async def fake_ollama_chat(messages, model, cfg):
        calls.append(messages)
        return "reacted!"

    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    first = await brain.process_screen_context("first thing", config, session_id="wp02-test")
    assert first.reply == "reacted!"
    assert len(calls) == 1

    second = await brain.process_screen_context("second thing", config, session_id="wp02-test")
    assert second.reply == ""
    assert len(calls) == 1  # no new model call while on cooldown


async def test_spoken_reply_saved_as_assistant_message_not_the_observation(tmp_path, monkeypatch):
    config = _config(tmp_path)

    async def fake_ollama_chat(messages, model, cfg):
        return "look at that"

    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    result = await brain.process_screen_context("something interesting", config, session_id="wp02-test")

    assert result.reply == "look at that"
    history = get_history(config.db_path, "wp02-test")
    assert [m["content"] for m in history] == ["look at that"]
    assert history[0]["role"] == "assistant"
