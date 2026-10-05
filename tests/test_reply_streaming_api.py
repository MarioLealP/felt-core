"""WP-54: streamed chat replies + the chunk long-poll endpoint + emotion
reaching felt-voice. felt-voice is faked by replacing `api.httpx.AsyncClient`
(after the test's own ASGI client exists)."""

import asyncio
from urllib.parse import quote, unquote

import httpx
import pytest

import felt_core.api as api
from felt_core import reply_stream, tts_cache
from felt_core.brain.result import ChatResult

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


class _FakeVoice:
    """Records each /v1/tts body; `fail_on` makes a text raise."""

    def __init__(self):
        self.bodies: list[dict] = []
        self.fail_on: set[str] = set()
        self.gate: asyncio.Event | None = None

    def client_class(self):
        voice = self

        class Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, *, json):
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
