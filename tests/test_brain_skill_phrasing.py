import felt_core.brain as brain
from felt_core.config import FeltConfig
from felt_core.plugin import SkillResult
from felt_core.plugin_loader import LoadedSkill


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


# -- _phrase_skill_result: the class -> route matrix -------------------


async def test_public_skill_routes_straight_to_cloud_no_privacy_check(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")
    privacy_calls = []

    async def fake_is_safe_for_cloud(text, cfg):
        privacy_calls.append(text)
        return True

    async def fake_cloud_generate(purpose, system, messages, cfg, **kwargs):
        return "cloud reply"

    monkeypatch.setattr(brain, "is_safe_for_cloud", fake_is_safe_for_cloud)
    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    reply = await brain._phrase_skill_result("weather?", "sunny 20C", [], "public", config)

    assert reply == "cloud reply"
    assert privacy_calls == []  # public never gates the tool output itself


async def test_google_ok_skill_routes_straight_to_cloud(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")

    async def fake_cloud_generate(purpose, system, messages, cfg, **kwargs):
        assert purpose == "skill_phrasing"
        return "cloud reply"

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    reply = await brain._phrase_skill_result("cal?", "dentist tuesday 3pm", [], "google_ok", config)

    assert reply == "cloud reply"


async def test_gated_skill_goes_to_cloud_when_the_result_passes_privacy(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")

    async def fake_is_safe_for_cloud(text, cfg):
        assert text == "the shelf plug is on"
        return True

    async def fake_cloud_generate(purpose, system, messages, cfg, **kwargs):
        return "cloud reply"

    monkeypatch.setattr(brain, "is_safe_for_cloud", fake_is_safe_for_cloud)
    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    reply = await brain._phrase_skill_result("shelf plug?", "the shelf plug is on", [], "gated", config)

    assert reply == "cloud reply"


async def test_gated_skill_stays_local_when_the_result_fails_privacy(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")

    async def fake_is_safe_for_cloud(text, cfg):
        return False

    async def fake_cloud_generate(*args, **kwargs):
        raise AssertionError("cloud must not be called when the gated check fails")

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        return "local reply"

    monkeypatch.setattr(brain, "is_safe_for_cloud", fake_is_safe_for_cloud)
    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)
    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    reply = await brain._phrase_skill_result("shelf plug?", "the shelf plug is on", [], "gated", config)

    assert reply == "local reply"


async def test_local_skill_never_calls_cloud_even_if_everything_would_say_safe(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")

    async def fake_is_safe_for_cloud(text, cfg):
        return True  # would say SAFE if ever asked - must never be asked for "local"

    async def fake_cloud_generate(*args, **kwargs):
        raise AssertionError("cloud must never be called for a 'local' skill")

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        return "local reply"

    monkeypatch.setattr(brain, "is_safe_for_cloud", fake_is_safe_for_cloud)
    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)
    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    reply = await brain._phrase_skill_result("find my notes", "found 3 notes", [], "local", config)

    assert reply == "local reply"


async def test_facts_never_appear_in_cloud_phrasing_messages(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")
    captured = {}

    async def fake_cloud_generate(purpose, system, messages, cfg, **kwargs):
        captured["system"] = system
        captured["messages"] = messages
        return "cloud reply"

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    from felt_core.db import save_fact

    save_fact(config.db_path, "likes pizza")
    await brain._phrase_skill_result("weather?", "sunny", [], "public", config)

    all_text = captured["system"] + " ".join(m["content"] for m in captured["messages"])
    assert "likes pizza" not in all_text


async def test_cloud_error_falls_back_to_local(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")

    async def fake_cloud_generate(*args, **kwargs):
        raise RuntimeError("boom")

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        return "local fallback"

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)
    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    reply = await brain._phrase_skill_result("weather?", "sunny", [], "public", config)

    assert reply == "local fallback"


# -- voice_line preference, wired through chat() ------------------------


async def test_voice_line_preferred_over_text_for_phrasing(tmp_path, monkeypatch):
    config = _config(tmp_path)
    fake_registry = {
        "dummy_skill": LoadedSkill(
            name="dummy_skill", description="", parameters_schema={}, handle=None, privacy="public"
        )
    }

    async def fake_decide_tool(message, registry, cfg):
        return ("dummy_skill", {})

    async def fake_run_skill(name, params, registry, ctx):
        return SkillResult(text="the long full text form", voice_line="short spoken form")

    captured = {}

    async def fake_phrase_skill_result(message, tool_output, history, skill_privacy, cfg, tone=None):
        captured["tool_output"] = tool_output
        return "phrased reply"

    monkeypatch.setattr(brain, "load_plugins", lambda plugins_dir: fake_registry)
    monkeypatch.setattr(brain, "decide_tool", fake_decide_tool)
    monkeypatch.setattr(brain, "run_skill", fake_run_skill)
    monkeypatch.setattr(brain, "_phrase_skill_result", fake_phrase_skill_result)

    result = await brain.chat("do the thing", config, session_id="wp06-test")

    assert captured["tool_output"] == "short spoken form"
    assert result.reply == "phrased reply"


# -- weather fast path ----------------------------------------------------


async def test_weather_fast_path_makes_no_skill_or_phrasing_calls(tmp_path, monkeypatch):
    config = _config(tmp_path)

    async def fake_decide_tool(message, registry, cfg):
        return ("get_weather", {"day": "now"})

    async def explode(*args, **kwargs):
        raise AssertionError("must not be called on the weather fast path")

    monkeypatch.setattr(brain, "decide_tool", fake_decide_tool)
    monkeypatch.setattr(brain, "run_skill", explode)
    monkeypatch.setattr(brain, "cloud_generate", explode)
    monkeypatch.setattr(brain, "_ollama_chat", explode)
    monkeypatch.setattr(brain, "is_safe_for_cloud", explode)

    brain._weather_precompute = {
        "line": "It's sunny, 20 degrees.",
        "animation_tag": "reaction/weather/sun",
        "computed_at": brain.time.monotonic(),
    }
    try:
        result = await brain.chat("what's the weather right now", config, session_id="wp06-test")
    finally:
        brain._weather_precompute = None

    assert result.reply == "It's sunny, 20 degrees."
    assert result.animation_tag == "reaction/weather/sun"


async def test_stale_precompute_does_not_take_the_fast_path(tmp_path, monkeypatch):
    config = _config(tmp_path)

    async def fake_decide_tool(message, registry, cfg):
        return ("get_weather", {"day": "now"})

    called = []

    async def fake_run_skill(name, params, registry, ctx):
        called.append(name)
        return SkillResult(text="fresh weather text")

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        return "fresh phrased reply"

    monkeypatch.setattr(brain, "decide_tool", fake_decide_tool)
    monkeypatch.setattr(brain, "run_skill", fake_run_skill)
    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    brain._weather_precompute = {
        "line": "stale line",
        "animation_tag": None,
        "computed_at": brain.time.monotonic() - brain._WEATHER_PRECOMPUTE_MAX_AGE_S - 1,
    }
    try:
        result = await brain.chat("what's the weather right now", config, session_id="wp06-test")
    finally:
        brain._weather_precompute = None

    assert called == ["get_weather"]
    assert result.reply == "fresh phrased reply"
