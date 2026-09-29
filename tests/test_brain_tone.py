"""WP-15: `tone` (felt-animation's GET /tone line) reaches the model for
chat and screen reactions. Checks that personality_prompt actually appends
it, and that it reaches the real model-call sites (not just accepted as a
parameter and dropped)."""

import felt_core.brain.orchestrator as orchestrator
import felt_core.brain.replies as brain
import felt_core.brain.screen_reactions as screen_reactions
from felt_core.config import FeltConfig

# -- personality_prompt ----------------------------------------------------


def test_personality_prompt_with_no_tone_is_unchanged():
    assert brain.personality_prompt(None) == brain.PERSONALITY_PROMPT


def test_personality_prompt_appends_tone():
    prompt = brain.personality_prompt("Hype-friend energy.")
    assert brain.PERSONALITY_PROMPT in prompt
    assert "Hype-friend energy." in prompt


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


async def test_react_to_screen_carries_tone_in_system(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = ['{"seen": "a game", "line": "nice!", "emotion": "happy"}']

    result = await screen_reactions.react_to_screen(
        b"jpeg", "game.exe", "Game", config, session_id="wp15-test", tone="She's a little annoyed."
    )

    assert result.reply == "nice!"
    assert "She's a little annoyed." in fake_llm.calls[0]["messages"][0]["content"]


# -- process_screen_context (the felt-vision path) --------------------------


async def test_process_screen_context_carries_tone(tmp_path, fake_llm):
    config = _config(tmp_path)

    await screen_reactions.process_screen_context(
        "a cat", config, session_id="wp15-test", tone="Cozy, relaxed."
    )

    assert "Cozy, relaxed." in fake_llm.calls[0]["messages"][0]["content"]
