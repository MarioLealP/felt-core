"""WP-47: plain home-control commands skip felt-llm's tool pick."""

import pytest

from felt_core.brain import orchestrator
from felt_core.brain.home_fast_path import home_fast_path
from felt_core.config import FeltConfig
from felt_core.plugin import SkillResult
from felt_core.plugin_loader import LoadedSkill


@pytest.mark.parametrize(
    ("message", "device", "action"),
    [
        ("turn off the shelf light please", "the shelf light", "off"),
        ("turn on my shelf light please :)", "my shelf light", "on"),
        ("Can you switch the fan on?", "the fan", "on"),
        ("could you please turn the lights back off", "the lights", "off"),
        ("Felt, turn off the chandelure", "the chandelure", "off"),
        ("turn front left on, thanks!", "front left", "on"),
        ("is the fan on?", "the fan", "status"),
        ("are the lights still on", "the lights", "status"),
    ],
)
def test_commands(message, device, action):
    assert home_fast_path(message) == ("smart_home_control", {"device": device, "action": action})


@pytest.mark.parametrize(
    "message",
    [
        "is the server on fire?",  # status without a device kind -> the model decides
        "I turned off my alarm this morning and overslept",
        "turn off the shelf light and tell me a joke about it",
        "turn off the light and dance",
        "can you turn around?",
        "how do I turn off notifications in KDE",
        "",
    ],
)
def test_not_commands(message):
    assert home_fast_path(message) is None


def _registry(handled):
    async def handle(params, ctx):
        handled.append(params)
        return SkillResult(text="Shelf turned off.")

    return {
        "smart_home_control": LoadedSkill(
            name="smart_home_control",
            description="home",
            parameters_schema={"type": "object", "properties": {"device": {"type": "string"}}},
            handle=handle,
        )
    }


async def test_command_calls_the_skill_without_the_models_pick(tmp_path, monkeypatch, fake_llm):
    handled = []
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: _registry(handled))
    monkeypatch.setattr(
        orchestrator, "phrase_skill_result", lambda *a, **k: _async("Done, the shelf is off.")
    )

    result = await orchestrator.chat(
        "turn off the shelf light please",
        FeltConfig(db_path=str(tmp_path / "felt.db")),
        session_id="s",
    )

    assert handled == [{"device": "the shelf light", "action": "off"}]
    assert fake_llm.calls == []  # no tool-picking round trip
    assert result.reply == "Done, the shelf is off."


async def _async(value):
    return value
