"""WP-15: `tone` (felt-animation's GET /tone line) threaded into the
system prompt for chat and screen reactions, cloud and local. Checks that
_personality_prompt actually appends it, and that it reaches the real
model-call sites (not just accepted as a parameter and dropped)."""

import felt_core.brain as brain
from felt_core.config import FeltConfig


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


# -- _personality_prompt ----------------------------------------------------


def test_personality_prompt_with_no_tone_is_unchanged():
    assert brain._personality_prompt(None) == brain._PERSONALITY_PROMPT


def test_personality_prompt_appends_tone():
    prompt = brain._personality_prompt("Hype-friend energy.")
    assert brain._PERSONALITY_PROMPT in prompt
    assert "Hype-friend energy." in prompt


# -- chat(): local path -------------------------------------------------


async def test_chat_local_reply_carries_tone_in_the_system_message(tmp_path, monkeypatch):
    config = _config(tmp_path)  # no gemini key -> local path
    captured = {}

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        captured["messages"] = messages
        return "a reply"

    async def fake_decide_tool(message, registry, cfg):
        return None

    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)
    monkeypatch.setattr(brain, "decide_tool", fake_decide_tool)

    await brain.chat("hello", config, session_id="wp15-test", tone="She's bored.")

    assert captured["messages"][0]["role"] == "system"
    assert "She's bored." in captured["messages"][0]["content"]


async def test_chat_local_reply_with_no_tone_carries_only_the_base_prompt(tmp_path, monkeypatch):
    config = _config(tmp_path)
    captured = {}

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        captured["messages"] = messages
        return "a reply"

    async def fake_decide_tool(message, registry, cfg):
        return None

    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)
    monkeypatch.setattr(brain, "decide_tool", fake_decide_tool)

    await brain.chat("hello", config, session_id="wp15-test")

    assert captured["messages"][0]["content"] == brain._PERSONALITY_PROMPT


# -- chat(): cloud path ---------------------------------------------------


async def test_chat_cloud_reply_carries_tone(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")
    captured = {}

    async def fake_cloud_generate(purpose, system, messages, cfg, **kwargs):
        captured["system"] = system
        return "cloud reply"

    async def fake_is_safe_for_cloud(text, cfg):
        return True

    async def fake_decide_tool(message, registry, cfg):
        return None

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)
    monkeypatch.setattr(brain, "is_safe_for_cloud", fake_is_safe_for_cloud)
    monkeypatch.setattr(brain, "decide_tool", fake_decide_tool)

    result = await brain.chat("hello", config, session_id="wp15-test", tone="A 2nd win in a row.")

    assert result.reply == "cloud reply"
    assert "A 2nd win in a row." in captured["system"]


# -- react_to_screen --------------------------------------------------------


async def test_react_to_screen_carries_tone_in_system(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")
    captured = {}

    async def fake_cloud_generate(purpose, system, messages, cfg, **kwargs):
        captured["system"] = system
        return '{"seen": "a game", "line": "nice!", "emotion": "happy"}'

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    result = await brain.react_to_screen(
        b"jpeg", "game.exe", "Game", config, session_id="wp15-test", tone="She's a little annoyed."
    )

    assert result.reply == "nice!"
    assert "She's a little annoyed." in captured["system"]


# -- process_screen_context (the felt-vision fallback path) -----------------


async def test_process_screen_context_local_path_carries_tone(tmp_path, monkeypatch):
    config = _config(tmp_path)
    captured = {}

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        captured["messages"] = messages
        return "a reaction"

    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    await brain.process_screen_context(
        "a cat", config, session_id="wp15-test", tone="Cozy, relaxed."
    )

    system_messages = [m["content"] for m in captured["messages"] if m["role"] == "system"]
    # _local_reply prepends its own system message ahead of the caller's
    # observations_block one - tone rides in that first system message.
    assert any("Cozy, relaxed." in content for content in system_messages)
