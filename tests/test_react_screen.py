import json

import pytest
from fastapi.testclient import TestClient

import felt_core.brain.screen_reactions as brain
from felt_core.api import app
from felt_core.config import FeltConfig
from felt_core.db import (
    get_recent_observations,
)
from felt_core.local_llm import LocalLLMUnavailable


def _config(tmp_path, **kwargs):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


def _json_response(seen="a game", line="Nice!", emotion="excited") -> str:
    return json.dumps({"seen": seen, "line": line, "emotion": emotion})


# -- the JSON contract + image reaching felt-llm -----------------------------


async def test_react_to_screen_happy_path(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = [
        _json_response(seen="a boss fight", line="Ooh, tough boss!", emotion="excited")
    ]

    jpeg_bytes = b"fake-jpeg-bytes"
    result = await brain.react_to_screen(
        jpeg_bytes, "game.exe", "My Game", config, session_id="wp07-test"
    )

    assert result.reply == "Ooh, tough boss!"
    assert result.emotion == "excited"
    call = fake_llm.calls[0]
    parts = call["messages"][-1]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert call["json_schema"] is not None

    observations = get_recent_observations(config.db_path, "wp07-test", limit=1)
    assert observations[0]["description"] == "a boss fight"
    assert observations[0]["spoken_reply"] == "Ooh, tough boss!"


async def test_unknown_emotion_folds_to_neutral(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = [_json_response(emotion="ecstatic")]  # not in the fixed set

    result = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-test2"
    )

    assert result.emotion == "neutral"


# -- cooldown + manual bypass ------------------------------------------------


async def test_cooldown_blocks_a_second_call_with_zero_model_calls(tmp_path, fake_llm):
    config = _config(tmp_path, screen_reaction_cooldown_s=90)
    fake_llm.default = _json_response()

    first = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-cooldown"
    )
    assert first.reply
    assert len(fake_llm.calls) == 1

    second = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-cooldown"
    )
    assert second.reply == ""
    assert len(fake_llm.calls) == 1  # no new model call while on cooldown


async def test_manual_trigger_bypasses_cooldown(tmp_path, fake_llm):
    config = _config(tmp_path, screen_reaction_cooldown_s=90)
    fake_llm.default = _json_response()

    await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-manual", trigger="focus"
    )
    assert len(fake_llm.calls) == 1

    # Still well within the 90s cooldown, but trigger="manual" skips it.
    result = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-manual", trigger="manual"
    )
    assert result.reply
    assert len(fake_llm.calls) == 2


# -- JSON parse failure retry -------------------------------------------------


async def test_json_parse_failure_retries_once(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = ["not json at all", _json_response(line="recovered")]

    result = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-retry"
    )

    assert result.reply == "recovered"
    assert len(fake_llm.calls) == 2


async def test_json_parse_failure_twice_gives_up_silently(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.default = "still not json"

    result = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-giveup"
    )

    assert result.reply == ""
    assert len(fake_llm.calls) == 2  # first attempt + exactly one retry, no more


async def test_lenient_extraction_of_json_block_from_extra_text(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = ["Sure! " + _json_response(line="extracted fine") + " Hope that helps!"]

    result = await brain.react_to_screen(
        b"jpeg", "app.exe", "Title", config, session_id="wp07-lenient"
    )

    assert result.reply == "extracted fine"


# -- LocalLLMUnavailable propagates (api.py maps it to a 503) ------------------


async def test_felt_llm_unavailable_propagates_uncaught(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.default = LocalLLMUnavailable("down")  # type: ignore[assignment]

    with pytest.raises(LocalLLMUnavailable):
        await brain.react_to_screen(
            b"jpeg", "app.exe", "Title", config, session_id="wp07-unavailable"
        )


# -- the endpoint maps it to a real 503 ---------------------------------------


def test_endpoint_maps_felt_llm_offline_to_503(tmp_path, monkeypatch, fake_llm):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    fake_llm.default = LocalLLMUnavailable("down")  # type: ignore[assignment]

    with TestClient(app) as client:
        response = client.post(
            "/v1/react/screen",
            files={"image": ("screen.jpg", b"fake-jpeg", "image/jpeg")},
            data={"app": "game.exe", "title": "My Game", "trigger": "focus"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "felt-llm offline"}
