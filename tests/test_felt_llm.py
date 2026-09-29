"""WP-36: felt-llm (llama.cpp) is the only model backend - the client's
payload shape, the chat/tool-call turn, screen reactions, the diary, the
warm-up loop, the reaction-tag endpoint, and the 503 contract when it is
down."""

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

import felt_core.api as api
import felt_core.brain.background as background
import felt_core.brain.orchestrator as orchestrator
import felt_core.brain.screen_reactions as screen
import felt_core.diary as diary
from felt_core import db, local_llm, metrics
from felt_core.config import FeltConfig
from felt_core.db import get_history
from felt_core.plugin import SkillResult
from felt_core.plugin_loader import LoadedSkill


def _config(tmp_path, **kwargs):
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


async def test_is_healthy_only_on_a_200(tmp_path, monkeypatch):
    statuses = iter([200, 503])
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(next(statuses))

    _mock_client(monkeypatch, handler)
    config = _config(tmp_path, local_llm_url="http://llm:8080/")
    assert await local_llm.is_healthy(config) is True
    assert await local_llm.is_healthy(config) is False
    assert seen == ["http://llm:8080/health"] * 2
    assert await local_llm.is_healthy(_config(tmp_path, local_llm_url=None)) is False


async def test_is_healthy_false_on_connection_error(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _mock_client(monkeypatch, handler)
    assert await local_llm.is_healthy(_config(tmp_path)) is False


# -- orchestrator.chat -------------------------------------------------------


def _skill(name, handle):
    return LoadedSkill(
        name=name,
        description=f"{name} skill",
        parameters_schema={"type": "object", "properties": {"when": {"type": "string"}}},
        handle=handle,
    )


def _tool_call(name, args):
    return {
        "id": "call1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


async def test_chat_local_reply_without_tool(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: {})
    fake_llm.script = ["  hello there "]

    result = await orchestrator.chat("hi", config, session_id="s1")

    assert result.reply == "hello there"
    assert len(fake_llm.calls) == 1
    call = fake_llm.calls[0]
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][-1] == {"role": "user", "content": "hi"}
    assert call["max_tokens"] == 160
    assert [m["content"] for m in get_history(config.db_path, "s1")][-1] == "hello there"


async def test_chat_local_tool_call_runs_skill_then_phrases(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    handled = []

    async def handle(params, ctx):
        handled.append(params)
        return SkillResult(text="12 degrees and sunny")

    monkeypatch.setattr(
        orchestrator, "load_plugins", lambda d: {"lookup_thing": _skill("lookup_thing", handle)}
    )
    fake_llm.script = [
        {"text": "", "tool_calls": [_tool_call("lookup_thing", {"when": "tomorrow"})]},
        "Sunny tomorrow, twelve degrees.",
    ]

    result = await orchestrator.chat("weather tomorrow?", config, session_id="s2")

    assert result.reply == "Sunny tomorrow, twelve degrees."
    assert handled == [{"when": "tomorrow"}]
    assert len(fake_llm.calls) == 2
    first, second = fake_llm.calls
    assert first["tools"][0]["function"]["name"] == "lookup_thing"
    # Same prefix, then the assistant tool_call and the tool result.
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert second["messages"][-2]["role"] == "assistant"
    assert second["messages"][-2]["tool_calls"][0]["id"] == "call1"
    assert second["messages"][-1]["role"] == "tool"
    assert second["messages"][-1]["tool_call_id"] == "call1"
    assert "12 degrees and sunny" in second["messages"][-1]["content"]


async def test_chat_local_guard_drops_an_unwarranted_tool_call(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)

    async def handle(params, ctx):
        raise AssertionError("guarded skill must not run")

    monkeypatch.setattr(
        orchestrator, "load_plugins", lambda d: {"generate_image": _skill("generate_image", handle)}
    )
    fake_llm.script = [
        {"text": "", "tool_calls": [_tool_call("generate_image", {})]},
        "Octopuses have three hearts.",
    ]

    result = await orchestrator.chat("tell me something fun about octopuses", config)

    assert result.reply == "Octopuses have three hearts."
    assert fake_llm.calls[1]["tools"] is None


async def test_chat_raises_when_felt_llm_is_down(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    monkeypatch.setattr(orchestrator, "load_plugins", lambda d: {})
    fake_llm.script = [local_llm.LocalLLMUnavailable("down")]

    with pytest.raises(local_llm.LocalLLMUnavailable):
        await orchestrator.chat("hi", config, session_id="s3")


async def test_chat_presence_fast_path_skips_the_model_turn(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    ran = []

    async def handle(params, ctx):
        ran.append(params)
        return SkillResult(text="ok, quiet")

    monkeypatch.setattr(
        orchestrator, "load_plugins", lambda d: {"felt_presence": _skill("felt_presence", handle)}
    )
    fake_llm.script = ["Fine, fine."]

    result = await orchestrator.chat("shut up", config, session_id="s4")

    assert ran == [{"intent": "shut_up"}]
    assert len(fake_llm.calls) == 1  # only the phrasing call, no tool-picking turn
    assert fake_llm.calls[0]["tools"] is None
    assert result.reply == "Fine, fine."


# -- screen reactions --------------------------------------------------------


def _screen_json(line="Nice one!", emotion="amused"):
    return json.dumps({"seen": "a game", "line": line, "emotion": emotion})


async def test_react_to_screen_local(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = [_screen_json()]

    result = await screen.react_to_screen(b"jpg", "game.exe", "My Game", config, "wp36")

    assert result.reply == "Nice one!"
    assert result.emotion == "amused"
    call = fake_llm.calls[0]
    assert [p["type"] for p in call["messages"][-1]["content"]] == ["text", "image_url"]
    assert set(call["json_schema"]["properties"]["emotion"]["enum"]) == set(screen._EMOTION_VALUES)


async def test_react_to_screen_raises_when_felt_llm_is_down(tmp_path, fake_llm):
    fake_llm.script = [local_llm.LocalLLMUnavailable("down")]

    with pytest.raises(local_llm.LocalLLMUnavailable):
        await screen.react_to_screen(b"jpg", "game.exe", "My Game", _config(tmp_path), "wp36b")


async def test_react_to_screen_parse_failure_twice_gives_no_reaction(tmp_path, fake_llm):
    fake_llm.default = "not json"

    result = await screen.react_to_screen(b"jpg", "a", "b", _config(tmp_path), "wp36c")

    assert len(fake_llm.calls) == 2
    assert result.reply == ""


# -- diary -------------------------------------------------------------------


async def test_diary_generation_uses_felt_llm(tmp_path, fake_llm):
    fake_llm.script = ['{"entry": "local"}']
    out = await diary._generate_json(_config(tmp_path), "sys", "material", max_tokens=100)
    assert out == '{"entry": "local"}'
    assert fake_llm.calls[0]["max_tokens"] == 100


async def test_diary_writer_skips_quietly_when_felt_llm_is_down(tmp_path, fake_llm, caplog):
    config = _config(tmp_path)
    db.save_event(
        config.db_path, "game_start", json.dumps({"name": "endfield.exe"}), "2026-09-27 12:00:00"
    )
    fake_llm.script = [local_llm.LocalLLMUnavailable("down")]

    with caplog.at_level(logging.INFO, logger="felt_core.diary"):
        written = await diary.write_diary_for_date(config, "2026-09-27")

    assert written is False
    assert db.get_diary(config.db_path, "2026-09-27") is None
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    assert any("felt-llm unavailable" in r.message for r in caplog.records)


# -- weather precompute ------------------------------------------------------


async def test_weather_precompute_phrases_with_felt_llm_and_skips_when_down(
    tmp_path, monkeypatch, fake_llm
):
    config = _config(tmp_path)

    async def handle(params, ctx):
        return SkillResult(text="15 degrees, cloudy")

    monkeypatch.setattr(
        background, "load_plugins", lambda d: {"get_weather": _skill("get_weather", handle)}
    )
    monkeypatch.setattr(background, "_weather_precompute", None)

    fake_llm.script = [local_llm.LocalLLMUnavailable("down")]
    await background.refresh_weather_precompute(config)
    assert background._weather_precompute is None  # skipped, nothing cached

    fake_llm.script = ["Cloudy out, fifteen degrees."]
    await background.refresh_weather_precompute(config)
    assert background._weather_precompute is not None
    assert background._weather_precompute["line"] == "Cloudy out, fifteen degrees."
    assert fake_llm.calls[-1]["tools"] is None
    assert "15 degrees, cloudy" in fake_llm.calls[-1]["messages"][-1]["content"]
    monkeypatch.setattr(background, "_weather_precompute", None)


# -- warm-up -----------------------------------------------------------------


class _StopAfter:
    """A stand-in for asyncio.sleep that cancels the loop on its nth call."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.calls = 0

    async def __call__(self, seconds):
        self.calls += 1
        if self.calls >= self.n:
            raise asyncio.CancelledError


async def test_warm_up_sends_the_real_persona_and_tools_prefix(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)

    async def handle(params, ctx):
        raise AssertionError("never runs")

    monkeypatch.setattr(
        background, "load_plugins", lambda d: {"lookup_thing": _skill("lookup_thing", handle)}
    )

    await background.warm_up_llm(config)

    call = fake_llm.calls[0]
    assert call["tools"][0]["function"]["name"] == "lookup_thing"
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][-1]["role"] == "user"


async def test_warmup_loop_warms_on_up_and_again_after_an_outage(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    health = iter([True, True, False, True])
    warmed = []

    async def healthy(cfg):
        return next(health)

    async def warm(cfg):
        warmed.append(1)

    stop_after_four = _StopAfter(4)
    monkeypatch.setattr(local_llm, "is_healthy", healthy)
    monkeypatch.setattr(background, "warm_up_llm", warm)
    monkeypatch.setattr(background.asyncio, "sleep", stop_after_four)

    with pytest.raises(asyncio.CancelledError):
        await background.llm_warmup_loop(config)

    assert len(warmed) == 2  # once at first-up, once after the outage


async def test_warmup_loop_survives_failures_and_retries(tmp_path, monkeypatch):
    config = _config(tmp_path)
    attempts = []

    async def healthy(cfg):
        return True

    async def warm(cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise local_llm.LocalLLMUnavailable("still loading")
        if len(attempts) == 2:
            raise RuntimeError("boom")

    stop_after_three = _StopAfter(3)
    monkeypatch.setattr(local_llm, "is_healthy", healthy)
    monkeypatch.setattr(background, "warm_up_llm", warm)
    monkeypatch.setattr(background.asyncio, "sleep", stop_after_three)

    with pytest.raises(asyncio.CancelledError):
        await background.llm_warmup_loop(config)

    assert len(attempts) == 3  # failed twice, retried each minute, third succeeded


async def test_warm_up_is_not_recorded_in_latency_metrics(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    monkeypatch.setattr(background, "load_plugins", lambda d: {})
    metrics.start_request("r1", "/v1/chat", config.db_path)

    await background.warm_up_llm(config)

    current = metrics._current.get()
    assert current is not None and current.stages == []


# -- POST /v1/reaction-tag ---------------------------------------------------


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_PLUGINS_DIR", str(tmp_path / "none"))
    return TestClient(api.app)


def test_reaction_tag_picks_a_tag_with_an_enum_schema(tmp_path, monkeypatch, fake_llm):
    fake_llm.script = ['{"tag": "emotion/happy"}']

    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/v1/reaction-tag",
            json={"text": "Nice one!", "tags": ["emotion/happy", "emotion/sad"]},
        )

    assert response.status_code == 200
    assert response.json() == {"tag": "emotion/happy"}
    call = fake_llm.calls[0]
    assert call["max_tokens"] == 32
    assert call["temperature"] == 0
    assert call["json_schema"] == {
        "type": "object",
        "properties": {"tag": {"type": "string", "enum": ["emotion/happy", "emotion/sad"]}},
        "required": ["tag"],
    }
    assert "Nice one!" in call["messages"][-1]["content"]


def test_reaction_tag_null_when_the_reply_cannot_be_parsed(tmp_path, monkeypatch, fake_llm):
    fake_llm.script = ["not json", '{"tag": "not/offered"}']

    with _client(tmp_path, monkeypatch) as client:
        first = client.post("/v1/reaction-tag", json={"text": "x", "tags": ["a"]})
        second = client.post("/v1/reaction-tag", json={"text": "x", "tags": ["a"]})

    assert first.json() == {"tag": None}
    assert second.json() == {"tag": None}


def test_reaction_tag_rejects_empty_tags(tmp_path, monkeypatch, fake_llm):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/v1/reaction-tag", json={"text": "x", "tags": []})

    assert response.status_code == 400
    assert fake_llm.calls == []


def test_reaction_tag_503_when_felt_llm_is_down(tmp_path, monkeypatch, fake_llm):
    fake_llm.script = [local_llm.LocalLLMUnavailable("down")]

    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/v1/reaction-tag", json={"text": "x", "tags": ["a"]})

    assert response.status_code == 503
    assert response.json() == {"detail": "felt-llm offline"}


# -- 503 contract on the model-backed endpoints ---------------------------------


def test_model_endpoints_answer_503_when_felt_llm_is_down(tmp_path, monkeypatch, fake_llm):
    fake_llm.default = local_llm.LocalLLMUnavailable("down")  # type: ignore[assignment]
    monkeypatch.setenv("FELT_VOICE_URL", "http://voice.invalid")
    expected = {"detail": "felt-llm offline"}

    with _client(tmp_path, monkeypatch) as client:
        assert client.post("/v1/chat", json={"message": "hi"}).status_code == 503
        assert client.post("/v1/chat", json={"message": "hi"}).json() == expected
        assert client.post("/v1/text-chat", json={"message": "hi"}).json() == expected
        assert client.post("/v1/context/screen", json={"description": "a cat"}).json() == expected
        screen_resp = client.post(
            "/v1/react/screen", files={"image": ("s.jpg", b"jpg", "image/jpeg")}, data={"app": "x"}
        )
        assert screen_resp.status_code == 503 and screen_resp.json() == expected
        event_resp = client.post(
            "/v1/react/event", json={"event": "afk_end", "details": {"duration_s": 4000}}
        )
        assert event_resp.status_code == 503 and event_resp.json() == expected
