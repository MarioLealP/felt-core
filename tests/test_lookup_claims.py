"""FELT-108: a reply that claims a lookup without a tool call is retried with
a tool required."""

import json

import pytest

from felt_core.brain import orchestrator
from felt_core.brain.lookup_claims import claims_a_lookup, claims_a_tool_use, claims_an_action
from felt_core.config import FeltConfig
from felt_core.plugin import SkillResult
from felt_core.plugin_loader import LoadedSkill


@pytest.mark.parametrize(
    "text",
    [
        "Checking that for you. Just a sec while I pull up the wiki... Okay, so a Chaos Orb",
        "Let me look that up!",
        "One sec, I'll search your documents for it.",
        "Pulling up the wiki now.",
        "Looking it up...",
    ],
)
def test_claims(text):
    assert claims_a_lookup(text)


@pytest.mark.parametrize(
    "text",
    [
        "A Chaos Orb rerolls an item's modifiers.",
        "Morning! Coffee first?",
        "",
        "That's the reroll orb. " * 10 + "Let me know if you want more.",
    ],
)
def test_not_claims(text):
    assert not claims_a_lookup(text)


@pytest.mark.parametrize(
    "text",
    [
        # the two live replies from 2026-10-17 (the shelf light never changed)
        "Consider it done. I'm flipping that switch for you now.",
        "Got it. Turning the shelf light off for you now.",
        "I'll switch the fan on.",
        "Dimming the lights down a bit.",
    ],
)
def test_action_claims(text):
    assert claims_an_action(text)
    assert claims_a_tool_use(text)


@pytest.mark.parametrize(
    "text",
    [
        "I'm going to turn in for the night.",
        "That song really turned my mood around.",
        "Morning! Coffee first?",
        "Ha, nice one. " * 15 + "Want me to turn the fan on?",
    ],
)
def test_not_action_claims(text):
    assert not claims_an_action(text)


def _config(tmp_path):
    return FeltConfig(db_path=str(tmp_path / "felt.db"))


def _call():
    return {
        "id": "c1",
        "type": "function",
        "function": {"name": "poe2_wiki", "arguments": json.dumps({"query": "chaos orb"})},
    }


def _registry(handled):
    async def handle(params, ctx):
        handled.append(params)
        return SkillResult(text="[Chaos Orb] Rerolls one random modifier.")

    return {
        "poe2_wiki": LoadedSkill(
            name="poe2_wiki",
            description="wiki",
            parameters_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            handle=handle,
        )
    }


async def test_claimed_lookup_is_retried_with_a_tool_required(tmp_path, monkeypatch, fake_llm):
    handled = []
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _registry(handled))
    fake_llm.script = [
        "Checking that for you, pulling up the wiki... a Chaos Orb is the reset button.",
        {"text": "", "tool_calls": [_call()]},
        "Per the wiki, a Chaos Orb rerolls one random modifier.",
    ]

    result = await orchestrator.chat("what does a chaos orb do", _config(tmp_path), session_id="s")

    assert handled == [{"query": "chaos orb"}]
    assert [c["tool_choice"] for c in fake_llm.calls] == [None, "required", None]
    assert result.reply == "Per the wiki, a Chaos Orb rerolls one random modifier."


async def test_plain_reply_is_not_retried(tmp_path, monkeypatch, fake_llm):
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _registry([]))
    fake_llm.script = ["A Chaos Orb rerolls modifiers."]

    result = await orchestrator.chat("chaos orb?", _config(tmp_path), session_id="s")

    assert len(fake_llm.calls) == 1
    assert result.reply == "A Chaos Orb rerolls modifiers."


async def test_forced_retry_without_a_tool_keeps_the_reply(tmp_path, monkeypatch, fake_llm, caplog):
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _registry([]))
    fake_llm.script = ["Let me check... it rerolls modifiers.", "still no tool"]

    result = await orchestrator.chat("chaos orb?", _config(tmp_path), session_id="s")

    assert result.reply == "Let me check... it rerolls modifiers."
    assert "forced retry still called no tool" in caplog.text


async def test_forced_retry_error_keeps_the_reply(tmp_path, monkeypatch, fake_llm, caplog):
    from felt_core import local_llm

    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _registry([]))
    fake_llm.script = [
        "Let me check... it rerolls modifiers.",
        local_llm.LocalLLMUnavailable("400"),
    ]

    result = await orchestrator.chat("chaos orb?", _config(tmp_path), session_id="s")

    assert result.reply == "Let me check... it rerolls modifiers."
    assert "forced retry failed" in caplog.text
