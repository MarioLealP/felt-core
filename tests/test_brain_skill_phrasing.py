import json
import time

import felt_core.brain.background as background
import felt_core.brain.orchestrator as orchestrator
import felt_core.brain.replies as brain
from felt_core.config import FeltConfig
from felt_core.db import save_fact
from felt_core.plugin import SkillResult
from felt_core.plugin_loader import LoadedSkill


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


async def _unused_handle(params, ctx):
    raise AssertionError("not expected to be called in these tests")


def _skill(name):
    return LoadedSkill(name=name, description="", parameters_schema={}, handle=_unused_handle)


def _tool_call(name, args):
    return {
        "id": "call1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# -- phrase_skill_result -------------------------------------------------


async def test_phrase_skill_result_is_one_plain_local_call_with_the_tool_output(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = ["local reply"]

    reply = await brain.phrase_skill_result("weather?", "sunny 20C", [], config)

    assert reply == "local reply"
    assert len(fake_llm.calls) == 1
    assert fake_llm.calls[0]["tools"] is None
    assert fake_llm.calls[0]["max_tokens"] == brain._REPLY_MAX_TOKENS
    assert "sunny 20C" in fake_llm.calls[0]["messages"][-1]["content"]


async def test_phrase_skill_result_carries_facts_locally(tmp_path, fake_llm):
    # Nothing leaves the LAN, so facts are fine in the prompt.
    config = _config(tmp_path)
    save_fact(config.db_path, "likes pizza")

    await brain.phrase_skill_result("weather?", "sunny", [], config)

    all_text = " ".join(str(m["content"]) for m in fake_llm.calls[0]["messages"])
    assert "likes pizza" in all_text


# -- voice_line preference, wired through chat() ------------------------


async def test_voice_line_preferred_over_text_for_phrasing(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    fake_registry = {"dummy_skill": _skill("dummy_skill")}

    async def fake_run_skill(name, params, registry, ctx):
        return SkillResult(text="the long full text form", voice_line="short spoken form")

    monkeypatch.setattr(orchestrator, "load_plugins", lambda plugins_dir: fake_registry)
    monkeypatch.setattr(orchestrator, "run_skill", fake_run_skill)
    fake_llm.script = [
        {"text": "", "tool_calls": [_tool_call("dummy_skill", {})]},
        "phrased reply",
    ]

    result = await orchestrator.chat("do the thing", config, session_id="wp06-test")

    tool_message = fake_llm.calls[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "short spoken form" in tool_message["content"]
    assert "the long full text form" not in tool_message["content"]
    assert result.reply == "phrased reply"


# -- weather fast path ----------------------------------------------------


async def test_weather_fast_path_makes_no_skill_or_phrasing_calls(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    monkeypatch.setattr(
        orchestrator, "load_plugins", lambda d: {"get_weather": _skill("get_weather")}
    )

    async def explode(*args, **kwargs):
        raise AssertionError("must not be called on the weather fast path")

    monkeypatch.setattr(orchestrator, "run_skill", explode)
    fake_llm.script = [{"text": "", "tool_calls": [_tool_call("get_weather", {"day": "now"})]}]

    background._weather_precompute = {
        "line": "It's sunny, 20 degrees.",
        "animation_tag": "reaction/weather/sun",
        "computed_at": time.monotonic(),
    }
    try:
        result = await orchestrator.chat(
            "what's the weather right now", config, session_id="wp06-test"
        )
    finally:
        background._weather_precompute = None

    assert result.reply == "It's sunny, 20 degrees."
    assert result.animation_tag == "reaction/weather/sun"
    assert len(fake_llm.calls) == 1  # only the tool-picking turn, no phrasing call


async def test_stale_precompute_does_not_take_the_fast_path(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    monkeypatch.setattr(
        orchestrator, "load_plugins", lambda d: {"get_weather": _skill("get_weather")}
    )
    called = []

    async def fake_run_skill(name, params, registry, ctx):
        called.append(name)
        return SkillResult(text="fresh weather text")

    monkeypatch.setattr(orchestrator, "run_skill", fake_run_skill)
    fake_llm.script = [
        {"text": "", "tool_calls": [_tool_call("get_weather", {"day": "now"})]},
        "fresh phrased reply",
    ]

    background._weather_precompute = {
        "line": "stale line",
        "animation_tag": None,
        "computed_at": time.monotonic() - background._WEATHER_PRECOMPUTE_MAX_AGE_S - 1,
    }
    try:
        result = await orchestrator.chat(
            "what's the weather right now", config, session_id="wp06-test"
        )
    finally:
        background._weather_precompute = None

    assert called == ["get_weather"]
    assert result.reply == "fresh phrased reply"
