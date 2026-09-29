import pytest

import felt_core.brain.screen_reactions as brain
from felt_core.config import FeltConfig
from felt_core.db import get_history
from felt_core.local_llm import LocalLLMUnavailable


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


async def test_screen_context_makes_one_plain_local_call(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = ["nice reaction"]

    result = await brain.process_screen_context("a cat on the desk", config, session_id="wp02-test")

    assert result.reply == "nice reaction"
    assert len(fake_llm.calls) == 1
    call = fake_llm.calls[0]
    assert call["tools"] is None and call["json_schema"] is None
    # The observation rides in the (merged) system message, the prompt is the last user turn.
    assert "a cat on the desk" in call["messages"][0]["content"]
    assert call["messages"][-1] == {"role": "user", "content": brain._SCREEN_REACTION_PROMPT}


async def test_screen_context_raises_when_felt_llm_is_down(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = [LocalLLMUnavailable("down")]

    with pytest.raises(LocalLLMUnavailable):
        await brain.process_screen_context("a cat", config, session_id="wp02-test")


async def test_none_reply_triggers_exactly_one_local_retry(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = ["NONE", "okay something real"]

    result = await brain.process_screen_context("something", config, session_id="wp02-test")

    assert result.reply == "okay something real"
    assert len(fake_llm.calls) == 2


async def test_still_empty_after_retry_returns_silent_and_stops(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.default = "NONE"

    result = await brain.process_screen_context("something", config, session_id="wp02-test")

    assert result.reply == ""
    assert len(fake_llm.calls) == 2  # first attempt + exactly one retry, no more


async def test_second_call_inside_cooldown_makes_zero_model_calls(tmp_path, fake_llm):
    config = _config(tmp_path, screen_reaction_cooldown_s=90)
    fake_llm.default = "reacted!"

    first = await brain.process_screen_context("first thing", config, session_id="wp02-test")
    assert first.reply == "reacted!"
    assert len(fake_llm.calls) == 1

    second = await brain.process_screen_context("second thing", config, session_id="wp02-test")
    assert second.reply == ""
    assert len(fake_llm.calls) == 1  # no new model call while on cooldown


async def test_spoken_reply_saved_as_assistant_message_not_the_observation(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.default = "look at that"

    result = await brain.process_screen_context(
        "something interesting", config, session_id="wp02-test"
    )

    assert result.reply == "look at that"
    history = get_history(config.db_path, "wp02-test")
    assert [m["content"] for m in history] == ["look at that"]
    assert history[0]["role"] == "assistant"
