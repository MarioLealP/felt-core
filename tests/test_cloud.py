from datetime import datetime
from zoneinfo import ZoneInfo

import felt_core.cloud as cloud
from felt_core.config import FeltConfig


def _config(tmp_path, **kwargs):
    return FeltConfig(
        db_path=str(tmp_path / "felt.db"),
        gemini_api_key="fake-key",
        cloud_model_conversation="conv-model",
        cloud_model_overflow="overflow-model",
        cloud_model_volume="volume-model",
        **kwargs,
    )


def setup_function() -> None:
    # These are module-level, process-lifetime caches by design (see
    # cloud.py) - reset between tests so one test's exhaustion/rate-limit
    # state can't leak into the next.
    cloud._no_system_instruction.clear()
    cloud._recent_calls.clear()
    cloud._recent_tokens.clear()
    cloud._exhausted_until.clear()


def _ok_response(text: str) -> tuple[int, dict]:
    return 200, {"candidates": [{"content": {"parts": [{"text": text}]}}]}


async def test_thinking_part_is_skipped_for_the_real_answer(tmp_path, monkeypatch):
    # Real bug, 2026-09-22 (WP-06): Gemma 4 models emit a hidden
    # "thought": true part ahead of the real answer - _extract_text used
    # to blindly grab parts[0], returning the raw chain-of-thought instead
    # of the actual reply whenever a model does this.
    config = _config(tmp_path)

    async def fake_post(model, payload, cfg):
        return 200, {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "*  Plan: say something about the weather...", "thought": True},
                            {"text": "It's lovely and clear out there!"},
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    result = await cloud.cloud_generate("conversation", "sys", [{"role": "user", "content": "hey"}], config)

    assert result == "It's lovely and clear out there!"


async def test_all_thinking_no_final_answer_yields_empty_string(tmp_path, monkeypatch):
    # The pathological case (truncated mid-thought, MAX_TOKENS) - better
    # an empty reply than reading the model's internal reasoning aloud.
    config = _config(tmp_path)

    async def fake_post(model, payload, cfg):
        return 200, {
            "candidates": [
                {"content": {"parts": [{"text": "*  Still thinking about it...", "thought": True}]}}
            ]
        }

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    result = await cloud.cloud_generate("conversation", "sys", [{"role": "user", "content": "hey"}], config)

    assert result == ""


async def test_purpose_routing_conversation_uses_conversation_model(tmp_path, monkeypatch):
    config = _config(tmp_path)
    seen_models = []

    async def fake_post(model, payload, cfg):
        seen_models.append(model)
        return _ok_response("hi")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    result = await cloud.cloud_generate("conversation", "sys", [{"role": "user", "content": "hey"}], config)

    assert result == "hi"
    assert seen_models == ["conv-model"]


async def test_purpose_routing_screen_reaction_uses_volume_model(tmp_path, monkeypatch):
    config = _config(tmp_path)
    seen_models = []

    async def fake_post(model, payload, cfg):
        seen_models.append(model)
        return _ok_response("nice")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    result = await cloud.cloud_generate("screen_reaction", "sys", [{"role": "user", "content": "hey"}], config)

    assert result == "nice"
    assert seen_models == ["volume-model"]


async def test_rpd_exhaustion_falls_back_to_overflow(tmp_path, monkeypatch):
    config = _config(tmp_path, cloud_limits='{"conv-model": {"rpd": 2}}')
    from felt_core.db import increment_cloud_usage

    pt_day = cloud._pacific_day()
    increment_cloud_usage(config.db_path, "conv-model", "conversation", pt_day)
    increment_cloud_usage(config.db_path, "conv-model", "conversation", pt_day)  # now at the rpd=2 limit

    seen_models = []

    async def fake_post(model, payload, cfg):
        seen_models.append(model)
        return _ok_response("from overflow")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    result = await cloud.cloud_generate("conversation", "sys", [{"role": "user", "content": "hey"}], config)

    assert result == "from overflow"
    assert seen_models == ["overflow-model"]


async def test_all_models_exhausted_raises_cloud_unavailable(tmp_path, monkeypatch):
    config = _config(tmp_path, cloud_limits='{"conv-model": {"rpd": 1}, "overflow-model": {"rpd": 1}}')
    from felt_core.db import increment_cloud_usage

    pt_day = cloud._pacific_day()
    increment_cloud_usage(config.db_path, "conv-model", "conversation", pt_day)
    increment_cloud_usage(config.db_path, "overflow-model", "conversation", pt_day)

    async def fake_post(model, payload, cfg):
        raise AssertionError("should never actually call out - both models are already over budget")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    try:
        await cloud.cloud_generate("conversation", "sys", [{"role": "user", "content": "hey"}], config)
        assert False, "expected CloudUnavailable"
    except cloud.CloudUnavailable:
        pass


async def test_429_marks_model_exhausted_until_pacific_midnight(tmp_path, monkeypatch):
    config = _config(tmp_path)
    frozen_now = datetime(2026, 9, 22, 23, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(cloud.time, "time", lambda: frozen_now.timestamp())

    calls = []

    async def fake_post(model, payload, cfg):
        calls.append(model)
        if model == "conv-model":
            return 429, {"error": {"message": "Daily quota exceeded for this model."}}
        return _ok_response("from overflow")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    result = await cloud.cloud_generate("conversation", "sys", [{"role": "user", "content": "hey"}], config)

    assert result == "from overflow"
    assert calls == ["conv-model", "overflow-model"]
    assert "conv-model" in cloud._exhausted_until
    until, reason = cloud._exhausted_until["conv-model"]
    assert reason == "rpd"
    expected_midnight = datetime(2026, 9, 23, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    assert until == expected_midnight

    # A second call within the same window skips the exhausted model
    # entirely without even attempting it again.
    calls.clear()
    result2 = await cloud.cloud_generate("conversation", "sys", [{"role": "user", "content": "hey"}], config)
    assert result2 == "from overflow"
    assert calls == ["overflow-model"]


async def test_429_without_daily_wording_assumes_rpm_and_expires_in_60s(tmp_path, monkeypatch):
    config = _config(tmp_path)

    async def fake_post(model, payload, cfg):
        if model == "conv-model":
            return 429, {"error": {"message": "Rate limit exceeded, please retry shortly."}}
        return _ok_response("from overflow")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    await cloud.cloud_generate("conversation", "sys", [{"role": "user", "content": "hey"}], config)

    until, reason = cloud._exhausted_until["conv-model"]
    assert reason == "rpm"
    assert until - cloud.time.time() <= 60.5


async def test_system_instruction_rejection_retries_and_is_remembered(tmp_path, monkeypatch):
    config = _config(tmp_path)
    payloads = []

    async def fake_post(model, payload, cfg):
        payloads.append(payload)
        if "systemInstruction" in payload:
            return 400, {"error": {"message": "Developer instruction is not enabled for this model."}}
        return _ok_response("worked without systemInstruction")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    result = await cloud.cloud_generate("conversation", "you are Felt", [{"role": "user", "content": "hi"}], config)

    assert result == "worked without systemInstruction"
    assert len(payloads) == 2
    assert "systemInstruction" in payloads[0]
    assert "systemInstruction" not in payloads[1]
    # The system text should have been folded into the first user turn.
    assert payloads[1]["contents"][0]["parts"][0]["text"] == "you are Felt"
    assert "conv-model" in cloud._no_system_instruction

    # Remembered for the next call - goes straight to the no-systemInstruction form.
    payloads.clear()
    await cloud.cloud_generate("conversation", "you are Felt", [{"role": "user", "content": "hi again"}], config)
    assert len(payloads) == 1
    assert "systemInstruction" not in payloads[0]


async def test_json_mode_rejection_falls_back_to_plain_text_instruction(tmp_path, monkeypatch):
    config = _config(tmp_path)
    payloads = []

    async def fake_post(model, payload, cfg):
        payloads.append(payload)
        if payload.get("generationConfig", {}).get("responseMimeType"):
            return 400, {"error": {"message": "responseMimeType is not supported for this model."}}
        return _ok_response('Sure thing! {"skill": "get_weather", "params": {}} - hope that helps!')

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    result = await cloud.cloud_generate(
        "routing", "route this", [{"role": "user", "content": "what's the weather"}], config, json_mode=True
    )

    assert result == '{"skill": "get_weather", "params": {}}'


async def test_image_payload_shape(tmp_path, monkeypatch):
    config = _config(tmp_path)
    captured = {}

    async def fake_post(model, payload, cfg):
        captured["payload"] = payload
        return _ok_response("a cat")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    await cloud.cloud_generate(
        "screen_reaction", "sys", [{"role": "user", "content": "what's this?"}], config, images=[b"fake-jpeg-bytes"]
    )

    parts = captured["payload"]["contents"][-1]["parts"]
    image_parts = [p for p in parts if "inline_data" in p]
    assert len(image_parts) == 1
    assert image_parts[0]["inline_data"]["mime_type"] == "image/jpeg"
    import base64

    assert base64.b64decode(image_parts[0]["inline_data"]["data"]) == b"fake-jpeg-bytes"


def test_pacific_day_rollover():
    # 23:59 Pacific on the 22nd and 00:01 Pacific on the 23rd must land on
    # different pt_day strings even though they're only 2 minutes apart.
    late = datetime(2026, 9, 22, 23, 59, tzinfo=ZoneInfo("America/Los_Angeles"))
    early = datetime(2026, 9, 23, 0, 1, tzinfo=ZoneInfo("America/Los_Angeles"))

    assert cloud._pacific_day(late) == "2026-09-22"
    assert cloud._pacific_day(early) == "2026-09-23"
