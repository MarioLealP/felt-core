"""WP-36 phase C': the local-first paths (felt-llm via llama.cpp) and the
fallback back to the old paths. The autouse conftest fixture turns
local_first off for every older test; these opt in explicitly."""

import json

import httpx
import pytest

import felt_core.brain.orchestrator as orchestrator
import felt_core.brain.screen_reactions as screen
import felt_core.diary as diary
from felt_core import local_llm
from felt_core.config import FeltConfig
from felt_core.db import get_history
from felt_core.plugin import SkillResult
from felt_core.plugin_loader import LoadedSkill
from felt_core.privacy import is_safe_for_cloud


def _config(tmp_path, **kwargs):
    kwargs.setdefault("local_first", True)
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


# -- local_llm.chat payload shape --------------------------------------------


def _mock_client(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(local_llm.httpx, "AsyncClient", factory)


async def test_chat_payload_shape_and_parsing(tmp_path, monkeypatch):
    config = _config(tmp_path, local_llm_url="http://llm:8080/")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "abc",
                                    "type": "function",
                                    "function": {"name": "lookup_thing", "arguments": '{"a": 1}'},
                                }
                            ],
                        }
                    }
                ],
                "timings": {"prompt_n": 12, "cache_n": 5},
            },
        )

    _mock_client(monkeypatch, handler)
    tools = [{"type": "function", "function": {"name": "lookup_thing"}}]
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "facts"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "look"}, local_llm.image_part(b"jpg")],
        },
    ]

    reply = await local_llm.chat(
        messages, max_tokens=50, temperature=0.2, tools=tools, json_schema=schema, config=config
    )

    body = seen["body"]
    assert seen["url"] == "http://llm:8080/v1/chat/completions"
    assert body["model"] == "felt"
    assert body["max_tokens"] == 50
    assert body["temperature"] == 0.2
    assert body["tools"] == tools
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == schema
    # Extra system entries are folded into the single leading one.
    assert [m["role"] for m in body["messages"]] == ["system", "user", "user"]
    assert body["messages"][0]["content"] == "persona\n\nfacts"
    image = body["messages"][2]["content"][1]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert reply.prompt_n == 12
    assert reply.cache_n == 5
    assert local_llm.parse_tool_arguments(reply.tool_calls[0]) == ("lookup_thing", {"a": 1})


async def test_chat_omits_optional_fields_and_raises_unavailable(tmp_path, monkeypatch):
    config = _config(tmp_path)
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "yo"}}]})
        return httpx.Response(503, text="loading")

    _mock_client(monkeypatch, handler)
    reply = await local_llm.chat([{"role": "user", "content": "hi"}], max_tokens=8, config=config)
    assert reply.text == "yo"
    assert "tools" not in bodies[0]
    assert "response_format" not in bodies[0]

    with pytest.raises(local_llm.LocalLLMUnavailable):
        await local_llm.chat([{"role": "user", "content": "hi"}], max_tokens=8, config=config)


async def test_chat_connection_error_and_no_url_are_unavailable(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _mock_client(monkeypatch, handler)
    with pytest.raises(local_llm.LocalLLMUnavailable):
        await local_llm.chat(
            [{"role": "user", "content": "hi"}], max_tokens=8, config=_config(tmp_path)
        )
    with pytest.raises(local_llm.LocalLLMUnavailable):
        await local_llm.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=8,
            config=_config(tmp_path, local_llm_url=None),
        )


# -- orchestrator.chat -------------------------------------------------------


def _skill(name, handle, privacy="local"):
    return LoadedSkill(
        name=name,
        description=f"{name} skill",
        parameters_schema={"type": "object", "properties": {"when": {"type": "string"}}},
        handle=handle,
        privacy=privacy,
    )


def _tool_call(name, args):
    return {
        "id": "call1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _forbid_old_path(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("old router/privacy path must not run")

    monkeypatch.setattr(orchestrator, "decide_tool", boom)
    monkeypatch.setattr(orchestrator, "_timed_privacy_check", boom)
    monkeypatch.setattr(orchestrator, "_chat_reply", boom)


async def test_chat_local_reply_without_tool(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: {})
    _forbid_old_path(monkeypatch)
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        return local_llm.LocalReply(text="  hello there ")

    monkeypatch.setattr(local_llm, "chat", fake_chat)

    result = await orchestrator.chat("hi", config, session_id="s1")

    assert result.reply == "hello there"
    assert len(calls) == 1
    messages, kwargs = calls[0]
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "hi"}
    assert kwargs["max_tokens"] == 160
    assert [m["content"] for m in get_history(config.db_path, "s1")][-1] == "hello there"


async def test_chat_local_tool_call_runs_skill_then_phrases(tmp_path, monkeypatch):
    config = _config(tmp_path)
    handled = []

    async def handle(params, ctx):
        handled.append(params)
        return SkillResult(text="12 degrees and sunny")

    monkeypatch.setattr(
        orchestrator, "load_plugins", lambda d: {"lookup_thing": _skill("lookup_thing", handle)}
    )
    _forbid_old_path(monkeypatch)
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) == 1:
            return local_llm.LocalReply(
                text="", tool_calls=[_tool_call("lookup_thing", {"when": "tomorrow"})]
            )
        return local_llm.LocalReply(text="Sunny tomorrow, twelve degrees.")

    monkeypatch.setattr(local_llm, "chat", fake_chat)

    result = await orchestrator.chat("weather tomorrow?", config, session_id="s2")

    assert result.reply == "Sunny tomorrow, twelve degrees."
    assert handled == [{"when": "tomorrow"}]
    assert len(calls) == 2
    first_messages, first_kwargs = calls[0]
    assert first_kwargs["tools"][0]["function"]["name"] == "lookup_thing"
    second_messages, _ = calls[1]
    # Same prefix, then the assistant tool_call and the tool result.
    assert second_messages[: len(first_messages)] == first_messages
    assert second_messages[-2]["role"] == "assistant"
    assert second_messages[-2]["tool_calls"][0]["id"] == "call1"
    assert second_messages[-1]["role"] == "tool"
    assert second_messages[-1]["tool_call_id"] == "call1"
    assert "12 degrees and sunny" in second_messages[-1]["content"]


async def test_chat_local_guard_drops_an_unwarranted_tool_call(tmp_path, monkeypatch):
    config = _config(tmp_path)

    async def handle(params, ctx):
        raise AssertionError("guarded skill must not run")

    monkeypatch.setattr(
        orchestrator, "load_plugins", lambda d: {"generate_image": _skill("generate_image", handle)}
    )
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return local_llm.LocalReply(text="", tool_calls=[_tool_call("generate_image", {})])
        return local_llm.LocalReply(text="Octopuses have three hearts.")

    monkeypatch.setattr(local_llm, "chat", fake_chat)

    result = await orchestrator.chat("tell me something fun about octopuses", config)

    assert result.reply == "Octopuses have three hearts."
    assert "tools" not in calls[1]


async def test_chat_falls_back_to_the_old_path_when_felt_llm_is_down(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: {})
    seen = {}

    async def down(messages, **kwargs):
        raise local_llm.LocalLLMUnavailable("down")

    async def fake_decide(message, registry, cfg):
        seen["decide_cfg"] = cfg
        return None

    async def fake_chat_reply(message, history, cfg, safe, tone=None):
        seen["reply_cfg"] = cfg
        return "legacy reply"

    monkeypatch.setattr(local_llm, "chat", down)
    monkeypatch.setattr(orchestrator, "decide_tool", fake_decide)
    monkeypatch.setattr(orchestrator, "_chat_reply", fake_chat_reply)

    result = await orchestrator.chat("hi", config, session_id="s3")

    assert result.reply == "legacy reply"
    assert seen["decide_cfg"].local_first is False
    assert seen["reply_cfg"].local_first is False


async def test_chat_phrasing_failure_does_not_rerun_the_skill(tmp_path, monkeypatch):
    config = _config(tmp_path)
    runs = []

    async def handle(params, ctx):
        runs.append(params)
        return SkillResult(text="done")

    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: {"do_it": _skill("do_it", handle)})
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return local_llm.LocalReply(text="", tool_calls=[_tool_call("do_it", {})])
        raise local_llm.LocalLLMUnavailable("down")

    async def fake_phrase(message, tool_output, history, privacy, cfg, tone=None):
        assert cfg.local_first is False
        return f"phrased: {tool_output}"

    monkeypatch.setattr(local_llm, "chat", fake_chat)
    monkeypatch.setattr(orchestrator, "_phrase_skill_result", fake_phrase)

    result = await orchestrator.chat("do it please", config)

    assert result.reply == "phrased: done"
    assert runs == [{}]


# -- screen reactions --------------------------------------------------------


def _screen_json(line="Nice one!", emotion="amused"):
    return json.dumps({"seen": "a game", "line": line, "emotion": emotion})


async def test_react_to_screen_local_first(tmp_path, monkeypatch):
    config = _config(tmp_path)
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return local_llm.LocalReply(text=_screen_json())

    async def no_cloud(*args, **kwargs):
        raise AssertionError("cloud must not be called")

    monkeypatch.setattr(local_llm, "chat", fake_chat)
    monkeypatch.setattr(screen, "cloud_generate", no_cloud)

    result = await screen.react_to_screen(b"jpg", "game.exe", "My Game", config, "wp36")

    assert result.reply == "Nice one!"
    assert result.emotion == "amused"
    part_types = [p["type"] for p in captured["messages"][-1]["content"]]
    assert part_types == ["text", "image_url"]
    schema = captured["kwargs"]["json_schema"]
    assert set(schema["properties"]["emotion"]["enum"]) == set(screen._EMOTION_VALUES)


async def test_react_to_screen_falls_back_to_cloud(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")

    async def down(messages, **kwargs):
        raise local_llm.LocalLLMUnavailable("down")

    async def fake_cloud(purpose, system, messages, cfg, images=None, **kwargs):
        assert images == [b"jpg"]
        return _screen_json(line="From the cloud")

    monkeypatch.setattr(local_llm, "chat", down)
    monkeypatch.setattr(screen, "cloud_generate", fake_cloud)

    result = await screen.react_to_screen(b"jpg", "game.exe", "My Game", config, "wp36b")

    assert result.reply == "From the cloud"


async def test_react_to_screen_local_parse_failure_twice_uses_cloud(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")
    attempts = []

    async def garbage(messages, **kwargs):
        attempts.append(1)
        return local_llm.LocalReply(text="not json")

    async def fake_cloud(purpose, system, messages, cfg, images=None, **kwargs):
        return _screen_json(line="Cloud saved it")

    monkeypatch.setattr(local_llm, "chat", garbage)
    monkeypatch.setattr(screen, "cloud_generate", fake_cloud)

    result = await screen.react_to_screen(b"jpg", "a", "b", config, "wp36c")

    assert len(attempts) == 2
    assert result.reply == "Cloud saved it"


# -- privacy + diary ---------------------------------------------------------


async def test_privacy_uses_local_llm_when_local_first(tmp_path, monkeypatch):
    config = _config(tmp_path)
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["kwargs"] = kwargs
        return local_llm.LocalReply(text="SAFE")

    monkeypatch.setattr(local_llm, "chat", fake_chat)

    assert await is_safe_for_cloud("tell me a joke", config) is True
    assert captured["kwargs"]["max_tokens"] == 4
    assert captured["kwargs"]["temperature"] == 0


async def test_privacy_fails_closed_when_felt_llm_is_down(tmp_path, monkeypatch):
    async def down(messages, **kwargs):
        raise local_llm.LocalLLMUnavailable("down")

    monkeypatch.setattr(local_llm, "chat", down)

    assert await is_safe_for_cloud("anything", _config(tmp_path)) is False


async def test_diary_generation_is_local_first_with_cloud_fallback(tmp_path, monkeypatch):
    config = _config(tmp_path, gemini_api_key="fake-key")
    cloud_calls = []

    async def fake_cloud(purpose, system, messages, cfg, **kwargs):
        cloud_calls.append(purpose)
        return '{"entry": "cloud"}'

    monkeypatch.setattr(diary, "cloud_generate", fake_cloud)

    async def local_ok(messages, **kwargs):
        return local_llm.LocalReply(text='{"entry": "local"}')

    monkeypatch.setattr(local_llm, "chat", local_ok)
    assert (
        await diary._generate_json(config, "sys", "material", max_tokens=100)
        == '{"entry": "local"}'
    )
    assert cloud_calls == []

    async def down(messages, **kwargs):
        raise local_llm.LocalLLMUnavailable("down")

    monkeypatch.setattr(local_llm, "chat", down)
    assert (
        await diary._generate_json(config, "sys", "material", max_tokens=100)
        == '{"entry": "cloud"}'
    )
    assert cloud_calls == ["summary"]
