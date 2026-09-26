import json

from fastapi.testclient import TestClient

import felt_core.brain as brain
from felt_core.api import app
from felt_core.config import FeltConfig
from felt_core.db import (
    get_recent_observations,
    seconds_since_last_spoken_reaction,
    set_observation_reply,
)


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), gemini_api_key="fake-key", **kwargs)


def _json_response(seen="a game", line="Nice!", emotion="excited") -> str:
    return json.dumps({"seen": seen, "line": line, "emotion": emotion})


# -- the JSON contract + image reaching cloud_generate -----------------------


async def test_react_to_screen_happy_path(tmp_path, monkeypatch):
    config = _config(tmp_path)
    captured = {}

    async def fake_cloud_generate(
        purpose, system, messages, cfg, images=None, json_mode=False, max_tokens=None
    ):
        captured["purpose"] = purpose
        captured["images"] = images
        captured["json_mode"] = json_mode
        return _json_response(seen="a boss fight", line="Ooh, tough boss!", emotion="excited")

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    jpeg_bytes = b"fake-jpeg-bytes"
    result = await brain.react_to_screen(
        jpeg_bytes, "game.exe", "My Game", config, session_id="wp07-test"
    )

    assert result.reply == "Ooh, tough boss!"
    assert result.emotion == "excited"
    assert captured["purpose"] == "screen_reaction"
    assert captured["images"] == [jpeg_bytes]
    assert captured["json_mode"] is True

    observations = get_recent_observations(config.db_path, "wp07-test", limit=1)
    assert observations[0]["description"] == "a boss fight"
    assert observations[0]["spoken_reply"] == "Ooh, tough boss!"


async def test_unknown_emotion_folds_to_neutral(tmp_path, monkeypatch):
    config = _config(tmp_path)

    async def fake_cloud_generate(
        purpose, system, messages, cfg, images=None, json_mode=False, max_tokens=None
    ):
        return _json_response(emotion="ecstatic")  # not in the fixed set

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    result = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-test2"
    )

    assert result.emotion == "neutral"


# -- cooldown + manual bypass ------------------------------------------------


async def test_cooldown_blocks_a_second_call_with_zero_model_calls(tmp_path, monkeypatch):
    config = _config(tmp_path, screen_reaction_cooldown_s=90)
    calls = []

    async def fake_cloud_generate(
        purpose, system, messages, cfg, images=None, json_mode=False, max_tokens=None
    ):
        calls.append(1)
        return _json_response()

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    first = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-cooldown"
    )
    assert first.reply
    assert len(calls) == 1

    second = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-cooldown"
    )
    assert second.reply == ""
    assert len(calls) == 1  # no new model call while on cooldown


async def test_manual_trigger_bypasses_cooldown(tmp_path, monkeypatch):
    config = _config(tmp_path, screen_reaction_cooldown_s=90)
    calls = []

    async def fake_cloud_generate(
        purpose, system, messages, cfg, images=None, json_mode=False, max_tokens=None
    ):
        calls.append(1)
        return _json_response()

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-manual", trigger="focus"
    )
    assert len(calls) == 1

    # Still well within the 90s cooldown, but trigger="manual" skips it.
    result = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-manual", trigger="manual"
    )
    assert result.reply
    assert len(calls) == 2


# -- JSON parse failure retry -------------------------------------------------


async def test_json_parse_failure_retries_once_without_json_mode(tmp_path, monkeypatch):
    config = _config(tmp_path)
    calls = []

    async def fake_cloud_generate(
        purpose, system, messages, cfg, images=None, json_mode=False, max_tokens=None
    ):
        calls.append(json_mode)
        if len(calls) == 1:
            return "not json at all"
        return _json_response(line="recovered")

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    result = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-retry"
    )

    assert result.reply == "recovered"
    assert calls == [True, False]  # first with json_mode, retry without


async def test_json_parse_failure_twice_gives_up_silently(tmp_path, monkeypatch):
    config = _config(tmp_path)
    calls = []

    async def fake_cloud_generate(
        purpose, system, messages, cfg, images=None, json_mode=False, max_tokens=None
    ):
        calls.append(1)
        return "still not json"

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    result = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-giveup"
    )

    assert result.reply == ""
    assert len(calls) == 2  # first attempt + exactly one retry, no more


async def test_lenient_extraction_of_json_block_from_extra_text(tmp_path, monkeypatch):
    config = _config(tmp_path)

    async def fake_cloud_generate(
        purpose, system, messages, cfg, images=None, json_mode=False, max_tokens=None
    ):
        return "Sure! " + _json_response(line="extracted fine") + " Hope that helps!"

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    result = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-lenient"
    )

    assert result.reply == "extracted fine"


# -- CloudUnavailable propagates (api.py maps it to a 503) -------------------


async def test_cloud_unavailable_propagates_uncaught(tmp_path, monkeypatch):
    from felt_core.cloud import CloudUnavailable

    config = _config(tmp_path)

    async def fake_cloud_generate(*args, **kwargs):
        raise CloudUnavailable("no models available")

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)

    try:
        await brain.react_to_screen(
            b"jpeg", "app.exe", "Title", config, session_id="wp07-unavailable"
        )
        assert False, "expected CloudUnavailable to propagate"
    except CloudUnavailable:
        pass


# -- the endpoint maps CloudUnavailable to a real 503 ------------------------


def test_endpoint_maps_cloud_unavailable_to_503(tmp_path, monkeypatch):
    from felt_core.cloud import CloudUnavailable

    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_GEMINI_API_KEY", "fake-key")

    async def fake_react_to_screen(*args, **kwargs):
        raise CloudUnavailable("no models available")

    import felt_core.api as api_module

    monkeypatch.setattr(api_module, "react_to_screen", fake_react_to_screen)

    with TestClient(app) as client:
        response = client.post(
            "/v1/react/screen",
            files={"image": ("screen.jpg", b"fake-jpeg", "image/jpeg")},
            data={"app": "game.exe", "title": "My Game", "trigger": "focus"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "cloud_unavailable"}
