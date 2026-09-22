import felt_core.api as api
from felt_core import tts_cache


class _FakeConfig:
    voice_url = "http://fake-voice"


class _ExplodingClient:
    async def post(self, url, json):
        raise AssertionError("must not call felt-voice on a TTS cache hit")


async def test_tts_cache_hit_skips_the_felt_voice_call():
    text = "cached weather reply"
    tts_cache.put(api.strip_for_tts(text), b"FAKE-WAV-BYTES")

    response = await api._synthesize_reply(text, None, None, _FakeConfig(), _ExplodingClient())

    assert response.body == b"FAKE-WAV-BYTES"


async def test_cache_miss_calls_felt_voice_and_warms_the_cache():
    text = "a brand new reply " + str(id(object()))  # guaranteed not already cached
    calls = []

    class FakeResponse:
        content = b"REAL-WAV-BYTES"

        def raise_for_status(self):
            pass

    class FakeClient:
        async def post(self, url, json):
            calls.append((url, json))
            return FakeResponse()

    response = await api._synthesize_reply(text, None, None, _FakeConfig(), FakeClient())

    assert response.body == b"REAL-WAV-BYTES"
    assert len(calls) == 1
    assert tts_cache.get(api.strip_for_tts(text)) == b"REAL-WAV-BYTES"
