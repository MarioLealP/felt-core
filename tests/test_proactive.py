"""WP-25: the proactive channel - enqueue_proactive/wait_for_next (pure
logic against a tmp db), and the four /v1/proactive/* endpoints."""

from fastapi.testclient import TestClient

import felt_core.api as api
from felt_core.config import FeltConfig
from felt_core.proactive import clamp_timeout, enqueue_proactive, wait_for_next


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


# -- clamp_timeout: pure ----------------------------------------------------


def test_clamp_timeout_within_range_is_unchanged():
    assert clamp_timeout(25) == 25


def test_clamp_timeout_floors_at_one_second():
    assert clamp_timeout(0) == 1
    assert clamp_timeout(-5) == 1


def test_clamp_timeout_caps_at_thirty_seconds():
    assert clamp_timeout(300) == 30


# -- enqueue_proactive / wait_for_next: pure async --------------------------


async def test_wait_for_next_returns_an_already_enqueued_item(tmp_path):
    config = _config(tmp_path)
    enqueue_proactive(config, "diary", "morning greeting", animation_tag="wave", emotion="happy")

    item = await wait_for_next(config, timeout_s=1)

    assert item is not None
    assert item == {
        "id": item["id"],
        "kind": "diary",
        "text": "morning greeting",
        "animation_tag": "wave",
        "emotion": "happy",
    }


async def test_wait_for_next_times_out_with_nothing_enqueued(tmp_path):
    config = _config(tmp_path)

    item = await wait_for_next(config, timeout_s=0.2)

    assert item is None


async def test_wait_for_next_never_returns_an_expired_item(tmp_path):
    config = _config(tmp_path)
    enqueue_proactive(config, "urgent", "too late", ttl_s=-10)

    item = await wait_for_next(config, timeout_s=0.2)

    assert item is None


async def test_wait_for_next_marks_the_item_delivered(tmp_path):
    config = _config(tmp_path)
    enqueue_proactive(config, "diary", "only once")

    first = await wait_for_next(config, timeout_s=1)
    second = await wait_for_next(config, timeout_s=0.2)

    assert first is not None
    assert second is None


async def test_wait_for_next_omits_animation_tag_and_emotion_when_unset(tmp_path):
    config = _config(tmp_path)
    enqueue_proactive(config, "diary", "plain")

    item = await wait_for_next(config, timeout_s=1)

    assert item is not None
    assert item["animation_tag"] is None
    assert item["emotion"] is None


# -- the endpoints -----------------------------------------------------------


def _client(tmp_path, monkeypatch, debug=False):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_VOICE_URL", "http://voice.invalid")
    monkeypatch.setenv("FELT_DEBUG", "1" if debug else "0")
    monkeypatch.delenv("FELT_SPOTIFY_REFRESH_TOKEN", raising=False)
    return TestClient(api.app)  # no lifespan - no background loops


def test_next_endpoint_returns_an_item(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, debug=True)
    client.post("/v1/proactive/debug", json={"kind": "diary", "text": "hi there"})

    response = client.get("/v1/proactive/next", params={"timeout": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "diary"
    assert body["text"] == "hi there"


def test_next_endpoint_204_on_timeout(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/v1/proactive/next", params={"timeout": 1})

    assert response.status_code == 204
    assert response.content == b""


def test_debug_endpoint_404_when_debug_disabled(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, debug=False)

    response = client.post("/v1/proactive/debug", json={"text": "hi"})

    assert response.status_code == 404


def test_debug_endpoint_works_when_debug_enabled(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, debug=True)

    response = client.post("/v1/proactive/debug", json={"text": "hi", "kind": "diary"})

    assert response.status_code == 200
    assert "id" in response.json()


def test_requeue_endpoint_makes_the_item_claimable_again(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, debug=True)
    item_id = client.post("/v1/proactive/debug", json={"kind": "diary", "text": "hi"}).json()["id"]
    client.get("/v1/proactive/next", params={"timeout": 1})  # delivers + claims it

    requeue_response = client.post(f"/v1/proactive/{item_id}/requeue")
    next_response = client.get("/v1/proactive/next", params={"timeout": 1})

    assert requeue_response.json() == {"requeued": True}
    assert next_response.status_code == 200
    assert next_response.json()["id"] == item_id


def test_requeue_endpoint_unknown_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post("/v1/proactive/999999/requeue")

    assert response.json() == {"requeued": False}


def test_audio_endpoint_404_for_unknown_item(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/v1/proactive/999999/audio")

    assert response.status_code == 404


def test_audio_endpoint_returns_wav_from_the_tts_cache(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, debug=True)
    monkeypatch.setattr(api.tts_cache, "get", lambda text, emotion=None: b"RIFF-fake-wav")
    item_id = client.post("/v1/proactive/debug", json={"kind": "diary", "text": "hi there"}).json()[
        "id"
    ]

    response = client.get(f"/v1/proactive/{item_id}/audio")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == b"RIFF-fake-wav"


def test_audio_endpoint_503_without_voice_url(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, debug=True)
    monkeypatch.delenv("FELT_VOICE_URL", raising=False)
    item_id = client.post("/v1/proactive/debug", json={"kind": "diary", "text": "hi there"}).json()[
        "id"
    ]

    response = client.get(f"/v1/proactive/{item_id}/audio")

    assert response.status_code == 503


async def test_enqueue_inside_the_loop_presynthesises_and_audio_joins_it(tmp_path, monkeypatch):
    """Accept test measured 2.1 s of on-demand TTS at /audio time; synthesis
    now starts at enqueue and a fetch mid-synthesis joins it (one call)."""
    import asyncio
    from types import SimpleNamespace

    from felt_core import proactive, tts_cache

    calls = []
    gate = asyncio.Event()

    async def fake_synth(config, tts_text, emotion=None):
        calls.append(tts_text)
        await gate.wait()
        tts_cache.put(tts_text, b"RIFFwav")
        return b"RIFFwav"

    monkeypatch.setattr(proactive, "_synthesize", fake_synth)
    monkeypatch.setattr(proactive.db, "insert_proactive", lambda *a, **k: 7)
    cfg = SimpleNamespace(db_path=str(tmp_path / "x.db"), voice_url="http://voice")
    text = "prewarm check, unique 42"
    assert proactive.enqueue_proactive(cfg, "test", text, ttl_s=60) == 7
    fetch = asyncio.create_task(proactive.proactive_audio(cfg, text))
    await asyncio.sleep(0)
    gate.set()
    assert await fetch == b"RIFFwav"
    assert len(calls) == 1  # the fetch joined the in-flight synthesis
