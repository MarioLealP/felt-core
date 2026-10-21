"""WP-54: streamed chat replies + the chunk long-poll endpoint + emotion
reaching felt-voice. felt-voice is faked by replacing `api.httpx.AsyncClient`
(after the test's own ASGI client exists)."""

import asyncio
import json
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

import httpx
import pytest

import felt_core.api as api
from felt_core import local_llm, reply_stream, tts_cache
from felt_core.brain.result import ChatResult
from felt_core.brain.streaming import ReplyMeta
from felt_core.plugin import UIAction

_LONG_REPLY = (
    "This is the first sentence of the reply, long enough. "
    "Here is the second sentence, also a decent length! "
    "And the third one closes it out nicely, does it not?"
)


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


class _FakeSTT(_FakeResponse):
    def __init__(self, text: str):
        super().__init__(b"")
        self.text = text

    def json(self):
        return {"text": self.text}


class _FakeVoice:
    """Records each /v1/tts body; `fail_on` makes a text raise."""

    def __init__(self):
        self.bodies: list[dict] = []
        self.fail_on: set[str] = set()
        self.gate: asyncio.Event | None = None
        self.heard = "what's for dinner"

    def client_class(self):
        voice = self

        class Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, *, json: dict | None = None, files=None):
                if url.endswith("/v1/stt"):
                    return _FakeSTT(voice.heard)
                assert json is not None
                voice.bodies.append(json)
                if voice.gate is not None and len(voice.bodies) > 1:
                    await voice.gate.wait()
                if json["text"] in voice.fail_on:
                    raise httpx.ConnectError("voice down")
                return _FakeResponse(b"WAV:" + json["text"].encode())

        return Client


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_VOICE_URL", "http://fake-voice")
    tts_cache._cache.clear()
    reply_stream.clear()
    voice = _FakeVoice()
    reply = {"text": _LONG_REPLY}

    async def fake_chat(*a, **k):
        return ChatResult(reply=reply["text"])

    monkeypatch.setattr(api, "chat", fake_chat)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://t"
    ) as client:
        monkeypatch.setattr(api.httpx, "AsyncClient", voice.client_class())
        yield client, voice, reply
    reply_stream.clear()


async def test_multi_sentence_text_chat_returns_chunk_zero_and_stream_headers(env):
    client, voice, _ = env
    r = await client.post("/v1/text-chat", json={"message": "hi"})
    assert r.status_code == 200
    assert unquote(r.headers["X-Reply-Text"]) == _LONG_REPLY
    n = int(r.headers["X-Reply-Chunks"])
    assert n >= 2
    stream_id = r.headers["X-Reply-Stream"]
    assert len(stream_id) == 32
    first_text = api.speech_chunks(api.strip_for_tts(_LONG_REPLY))[0]
    assert r.content == b"WAV:" + first_text.encode()
    assert voice.bodies[0] == {"text": first_text}  # chat replies carry no emotion


async def test_one_sentence_reply_has_no_stream_headers(env):
    client, _, reply = env
    reply["text"] = "Just one short sentence."
    r = await client.post("/v1/text-chat", json={"message": "hi"})
    assert r.status_code == 200
    assert "X-Reply-Stream" not in r.headers
    assert "X-Reply-Chunks" not in r.headers
    assert r.content == b"WAV:Just one short sentence."


async def test_streaming_disabled_by_config(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("FELT_REPLY_STREAMING", "false")
    r = await client.post("/v1/text-chat", json={"message": "hi"})
    assert "X-Reply-Stream" not in r.headers
    assert r.content == b"WAV:" + api.strip_for_tts(_LONG_REPLY).encode()


async def test_chunk_endpoint_serves_the_rest_then_204_then_404(env):
    client, _, _ = env
    r = await client.post("/v1/text-chat", json={"message": "hi"})
    sid, n = r.headers["X-Reply-Stream"], int(r.headers["X-Reply-Chunks"])
    texts = api.speech_chunks(api.strip_for_tts(_LONG_REPLY))
    for i in range(1, n):
        c = await client.get(f"/v1/reply/{sid}/chunk/{i}")
        assert c.status_code == 200
        assert c.headers["content-type"] == "audio/wav"
        assert unquote(c.headers["X-Chunk-Text"]) == texts[i]
        assert c.content == b"WAV:" + texts[i].encode()
    assert (await client.get(f"/v1/reply/{sid}/chunk/{n}")).status_code == 204
    assert (await client.get("/v1/reply/nope/chunk/1")).status_code == 404


async def test_chunk_endpoint_408_when_not_ready_by_timeout(env):
    client, voice, _ = env
    voice.gate = asyncio.Event()  # chunks after 0 never finish
    r = await client.post("/v1/text-chat", json={"message": "hi"})
    sid = r.headers["X-Reply-Stream"]
    # timeout is clamped to >= 1 s, so this really waits about a second.
    c = await client.get(f"/v1/reply/{sid}/chunk/1", params={"timeout": 0})
    assert c.status_code == 408
    voice.gate.set()
    assert (await client.get(f"/v1/reply/{sid}/chunk/1")).status_code == 200


async def test_chunk_endpoint_502_when_that_chunk_failed(env):
    client, voice, _ = env
    texts = api.speech_chunks(api.strip_for_tts(_LONG_REPLY))
    voice.fail_on = {texts[1]}
    r = await client.post("/v1/text-chat", json={"message": "hi"})
    sid = r.headers["X-Reply-Stream"]
    assert (await client.get(f"/v1/reply/{sid}/chunk/1")).status_code == 502


async def test_chunk_endpoint_is_not_latency_timed():
    assert api._is_untimed("/v1/reply/abc/chunk/1")


async def test_tts_cache_is_keyed_on_emotion():
    tts_cache._cache.clear()
    tts_cache.put("hello there", b"neutral")
    tts_cache.put("hello there", b"sleepy", "sleepy")
    assert tts_cache.get("hello there") == b"neutral"
    assert tts_cache.get("hello there", None) == b"neutral"
    assert tts_cache.get("hello there", "sleepy") == b"sleepy"
    assert tts_cache.get("hello there", "excited") is None


async def test_emotion_reaches_the_felt_voice_body_for_screen_reactions(env, monkeypatch):
    client, voice, _ = env

    async def fake_react(*a, **k):
        return ChatResult(reply="Wow, look at that go.", emotion="excited")

    monkeypatch.setattr(api, "react_to_screen", fake_react)
    r = await client.post(
        "/v1/react/screen",
        files={"image": ("s.jpg", b"jpg", "image/jpeg")},
        data={"app": "game.exe", "title": "Game"},
    )
    assert r.status_code == 200
    assert r.headers["X-Emotion"] == quote("excited")
    assert voice.bodies == [{"text": "Wow, look at that go.", "emotion": "excited"}]
    assert "X-Reply-Stream" not in r.headers  # screen reactions never stream


async def test_stream_headers_reach_same_origin_clients_without_cors(env):
    """The app is same-origin (WP-62 0.2): the stream headers are there, and
    no CORS header exposes anything to another origin."""
    client, _, _ = env
    r = await client.post(
        "/v1/text-chat", json={"message": "hi"}, headers={"Origin": "http://dash"}
    )
    assert not any(h.lower().startswith("access-control-") for h in r.headers)


async def test_voice_down_still_returns_her_text(env):
    """felt-voice off (the laptop): 200 with her reply text and no audio, not a 500."""
    client, voice, _ = env
    first_text = api.speech_chunks(api.strip_for_tts(_LONG_REPLY))[0]
    voice.fail_on.add(first_text)
    r = await client.post("/v1/text-chat", json={"message": "hi"})
    assert r.status_code == 200
    assert unquote(r.headers["X-Reply-Text"]) == _LONG_REPLY
    assert r.headers["X-Voice"] == "unavailable"
    assert r.content == b""
    assert "X-Reply-Stream" not in r.headers


# -- WP-65: protocol v2 (X-Accept-Stream: 2) -----------------------------------

_V2 = {"X-Accept-Stream": "2"}
_CHUNKS = [
    "Fried rice! You've got everything for it.",
    "Day-old rice is *actually* better, it fries without going mushy.",
    "Soy sauce at the end.",
]


@dataclass
class _Script:
    """chat_streamed() faked: yields `chunks` (chunk 1 waits for `gate` if
    set), then raises `error` or yields `result`."""

    chunks: list[str] = field(default_factory=lambda: list(_CHUNKS))
    result: ChatResult = field(
        default_factory=lambda: ChatResult(reply=" ".join(_CHUNKS), animation_tag="emotion/happy")
    )
    gate: asyncio.Event | None = None
    error: Exception | None = None
    seen: list[str] = field(default_factory=list)
    meta: ReplyMeta | None = None


@pytest.fixture
def streamed(env, monkeypatch):
    script = _Script()

    async def fake(message, config, session_id="default", tone=None, answering=None):
        script.seen.append(message)
        if script.meta is not None:
            yield script.meta
        for i, chunk in enumerate(script.chunks):
            if i == 1 and script.gate is not None:
                await script.gate.wait()
            yield chunk
        if script.error is not None:
            raise script.error
        yield script.result

    monkeypatch.setattr(api, "chat_streamed", fake)
    return script


async def test_without_the_v2_header_nothing_changes(env, streamed):
    client, _, _ = env
    r = await client.post("/v1/text-chat", json={"message": "hi"})
    assert r.headers["X-Reply-Chunks"].isdigit()
    assert "X-Reply-Stream-Version" not in r.headers
    assert streamed.seen == []


async def test_v2_answers_with_chunk_zero_then_the_rest_as_it_comes(env, streamed):
    client, voice, _ = env
    streamed.gate = asyncio.Event()
    r = await client.post("/v1/text-chat", json={"message": "dinner?"}, headers=_V2)

    assert r.status_code == 200
    assert r.headers["X-Reply-Stream-Version"] == "2"
    assert r.headers["X-Reply-Chunks"] == "open"
    assert unquote(r.headers["X-Reply-Text"]) == _CHUNKS[0]
    assert r.content == b"WAV:" + _CHUNKS[0].encode()
    assert "X-Animation-Tag" not in r.headers  # not known yet - see /v1/reply/{id}
    sid = r.headers["X-Reply-Stream"]

    # still writing: chunk 1 doesn't exist yet, the reply isn't done
    assert (await client.get(f"/v1/reply/{sid}/chunk/1?timeout=1")).status_code == 408
    assert (await client.get(f"/v1/reply/{sid}")).json() == {"done": False, "text": _CHUNKS[0]}

    streamed.gate.set()
    c1 = await client.get(f"/v1/reply/{sid}/chunk/1")
    assert c1.status_code == 200
    assert unquote(c1.headers["X-Chunk-Text"]) == _CHUNKS[1]
    assert voice.bodies[1] == {"text": api.strip_for_tts(_CHUNKS[1])}  # stripped for TTS
    assert (await client.get(f"/v1/reply/{sid}/chunk/2")).content == b"WAV:" + _CHUNKS[2].encode()
    assert (await client.get(f"/v1/reply/{sid}/chunk/3")).status_code == 204
    assert (await client.get(f"/v1/reply/{sid}")).json() == {
        "done": True,
        "text": " ".join(_CHUNKS),
        "ui_action": None,
        "animation_tag": "emotion/happy",
        "emotion": None,
    }
    assert (await client.get("/v1/reply/nope")).status_code == 404


async def test_v2_chunk_long_poll_waits_for_a_chunk_still_being_written(env, streamed):
    client, _, _ = env
    streamed.gate = asyncio.Event()
    r = await client.post("/v1/text-chat", json={"message": "dinner?"}, headers=_V2)
    sid = r.headers["X-Reply-Stream"]
    poll = asyncio.create_task(client.get(f"/v1/reply/{sid}/chunk/1?timeout=10"))
    await asyncio.sleep(0.05)
    assert not poll.done()
    streamed.gate.set()
    assert (await poll).status_code == 200


async def test_v2_reply_with_no_chunks_is_the_whole_response(env, streamed):
    client, _, _ = env
    streamed.chunks = []
    streamed.result = ChatResult(reply="Okay.", animation_tag="emotion/happy")
    r = await client.post("/v1/text-chat", json={"message": "hi"}, headers=_V2)
    assert "X-Reply-Stream" not in r.headers
    assert unquote(r.headers["X-Reply-Text"]) == "Okay."
    assert unquote(r.headers["X-Animation-Tag"]) == "emotion/happy"


async def test_v2_voice_down_still_returns_her_whole_text(env, streamed):
    client, voice, _ = env
    voice.fail_on = {_CHUNKS[0], " ".join(_CHUNKS).replace("*", "")}
    r = await client.post("/v1/text-chat", json={"message": "hi"}, headers=_V2)
    assert r.status_code == 200
    assert r.headers["X-Voice"] == "unavailable"
    assert unquote(r.headers["X-Reply-Text"]) == " ".join(_CHUNKS)
    assert "X-Reply-Stream" not in r.headers


async def test_v2_felt_llm_down_before_a_word_is_503(env, streamed):
    client, _, _ = env
    streamed.chunks = []
    streamed.error = local_llm.LocalLLMUnavailable("down")
    r = await client.post("/v1/text-chat", json={"message": "hi"}, headers=_V2)
    assert r.status_code == 503


async def test_v2_felt_llm_gone_mid_reply_ends_the_stream(env, streamed):
    client, _, _ = env
    streamed.chunks = _CHUNKS[:2]
    streamed.error = local_llm.LocalLLMUnavailable("down")
    r = await client.post("/v1/text-chat", json={"message": "hi"}, headers=_V2)
    sid = r.headers["X-Reply-Stream"]
    assert (await client.get(f"/v1/reply/{sid}/chunk/1")).status_code == 200
    assert (await client.get(f"/v1/reply/{sid}/chunk/2")).status_code == 204
    body = (await client.get(f"/v1/reply/{sid}")).json()
    assert body["done"] and body["error"] == "felt-llm offline"
    assert body["text"] == " ".join(_CHUNKS[:2])


async def test_v2_voice_chat_streams_too(env, streamed):
    client, _, _ = env
    r = await client.post(
        "/v1/voice-chat", files={"file": ("a.wav", b"RIFF", "audio/wav")}, headers=_V2
    )
    assert r.status_code == 200
    assert r.headers["X-Reply-Chunks"] == "open"
    assert unquote(r.headers["X-Transcribed-Text"]) == "what's for dinner"
    assert streamed.seen == ["what's for dinner"]


async def test_v2_chunk_zero_carries_the_skills_tag_and_action(env, streamed):
    client, _, _ = env
    streamed.meta = ReplyMeta(
        ui_action=UIAction(kind="presence", payload={"action": "hush"}), animation_tag="emotion/sad"
    )
    r = await client.post("/v1/text-chat", json={"message": "shut up"}, headers=_V2)
    assert r.headers["X-Reply-Chunks"] == "open"
    assert unquote(r.headers["X-Animation-Tag"]) == "emotion/sad"
    action = json.loads(unquote(r.headers["X-UI-Action"]))
    assert action == {"kind": "presence", "payload": {"action": "hush"}}
    sid = r.headers["X-Reply-Stream"]
    assert (await client.get(f"/v1/reply/{sid}/chunk/3")).status_code == 204
