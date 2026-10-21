"""WP-15: `tone` (felt-animation's GET /tone line) reaches the model for
chat and screen reactions - on the user turn, never in the system prompt,
which is the day's fixed persona so felt-llm caches it (2026-10-21). Checks
that it reaches the real model-call sites (not just accepted and dropped)."""

import felt_core.brain.orchestrator as orchestrator
import felt_core.brain.replies as brain
import felt_core.brain.screen_reactions as screen_reactions
from felt_core.config import FeltConfig

# -- daily_persona / per_call_context -----------------------------------------


def test_the_system_prompt_is_the_same_every_call():
    assert brain.daily_persona() == brain.daily_persona()


def test_per_call_context_carries_the_tone():
    assert brain.per_call_context(None) == ""
    assert brain.per_call_context("Hype-friend energy.") == "Right now: Hype-friend energy."
    assert brain.with_context("", "hi") == "hi"
    assert brain.with_context("Right now: x", "hi") == "Right now: x\n\nhi"


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


# -- chat() ------------------------------------------------------------------


async def test_chat_carries_tone_on_the_user_turn_not_the_cached_system_message(tmp_path, fake_llm):
    config = _config(tmp_path, plugins_dir=str(tmp_path / "none"))

    await orchestrator.chat("hello", config, session_id="wp15-test", tone="She's bored.")

    messages = fake_llm.calls[0]["messages"]
    # local_turn_messages keeps the system message the day's fixed persona
    # (cache-stable); the tone rides on the latest user message.
    assert messages[0]["role"] == "system"
    assert "She's bored." not in messages[0]["content"]
    assert "Right now: She's bored." in messages[-1]["content"]


async def test_chat_with_no_tone_carries_only_the_base_prompt(tmp_path, fake_llm):
    config = _config(tmp_path, plugins_dir=str(tmp_path / "none"))

    await orchestrator.chat("hello", config, session_id="wp15-test")

    messages = fake_llm.calls[0]["messages"]
    assert messages[0]["content"] == brain._daily_persona()
    assert "Right now:" not in messages[-1]["content"]


# -- react_to_screen --------------------------------------------------------


async def test_react_to_screen_carries_tone_on_the_user_turn(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = ['{"seen": "a game", "line": "nice!", "emotion": "happy"}']

    result = await screen_reactions.react_to_screen(
        b"jpeg", "game.exe", "Game", config, session_id="wp15-test", tone="She's a little annoyed."
    )

    assert result.reply == "nice!"
    messages = fake_llm.calls[0]["messages"]
    assert messages[0]["content"] == brain.daily_persona()
    user = messages[-1]["content"]
    text = user if isinstance(user, str) else user[0]["text"]
    assert "Right now: She's a little annoyed." in text


# -- process_screen_context (the felt-vision path) --------------------------


async def test_process_screen_context_carries_tone(tmp_path, fake_llm):
    config = _config(tmp_path)

    await screen_reactions.process_screen_context(
        "a cat", config, session_id="wp15-test", tone="Cozy, relaxed."
    )

    messages = fake_llm.calls[0]["messages"]
    assert "Cozy, relaxed." not in messages[0]["content"]
    assert "Cozy, relaxed." in messages[-1]["content"]
