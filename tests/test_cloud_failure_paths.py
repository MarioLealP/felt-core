"""cloud.cloud_generate failure handling and governor edges found by the
2026-09-24 audit: every failure surfaces as CloudUnavailable (so callers'
fallbacks run), the screen_reaction share applies to the overflow model
too, and "system"-role messages never reach the API as user turns."""

import httpx

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
    cloud._no_system_instruction.clear()
    cloud._recent_calls.clear()
    cloud._recent_tokens.clear()
    cloud._exhausted_until.clear()


def _ok_response(text: str) -> tuple[int, dict]:
    return 200, {"candidates": [{"content": {"parts": [{"text": text}]}}]}


async def _expect_unavailable(coro) -> None:
    try:
        await coro
    except cloud.CloudUnavailable:
        return
    raise AssertionError("expected CloudUnavailable")


async def test_non_429_error_tries_overflow_then_raises_cloud_unavailable(tmp_path, monkeypatch):
    config = _config(tmp_path)
    seen = []

    async def fake_post(model, payload, cfg):
        seen.append(model)
        return 500, {"error": {"message": "internal"}}

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    await _expect_unavailable(
        cloud.cloud_generate("screen_reaction", "sys", [{"role": "user", "content": "hey"}], config)
    )
    assert seen == ["volume-model", "overflow-model"]


async def test_non_429_error_on_volume_falls_back_to_overflow(tmp_path, monkeypatch):
    config = _config(tmp_path)

    async def fake_post(model, payload, cfg):
        if model == "volume-model":
            return 503, {"error": {"message": "overloaded"}}
        return _ok_response("from overflow")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    result = await cloud.cloud_generate(
        "skill_phrasing", "sys", [{"role": "user", "content": "hey"}], config
    )
    assert result == "from overflow"


async def test_network_error_surfaces_as_cloud_unavailable(tmp_path, monkeypatch):
    config = _config(tmp_path)

    async def fake_post(model, payload, cfg):
        raise httpx.ConnectTimeout("slow")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    await _expect_unavailable(
        cloud.cloud_generate("conversation", "sys", [{"role": "user", "content": "hey"}], config)
    )


async def test_screen_share_cap_also_applies_to_the_overflow_model(tmp_path, monkeypatch):
    config = _config(
        tmp_path, cloud_limits='{"volume-model": {"rpd": 10}, "overflow-model": {"rpd": 10}}'
    )
    from felt_core.db import increment_cloud_usage

    pt_day = cloud._pacific_day()
    for model in ("volume-model", "overflow-model"):
        for _ in range(6):  # 60% of rpd=10
            increment_cloud_usage(config.db_path, model, "screen_reaction", pt_day)

    async def fake_post(model, payload, cfg):
        raise AssertionError("screen traffic is over its share on both models")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    await _expect_unavailable(
        cloud.cloud_generate("screen_reaction", "sys", [{"role": "user", "content": "hey"}], config)
    )


async def test_other_purposes_still_use_overflow_after_screen_share_is_spent(tmp_path, monkeypatch):
    config = _config(
        tmp_path, cloud_limits='{"volume-model": {"rpd": 10}, "overflow-model": {"rpd": 10}}'
    )
    from felt_core.db import increment_cloud_usage

    pt_day = cloud._pacific_day()
    for _ in range(6):
        increment_cloud_usage(config.db_path, "overflow-model", "screen_reaction", pt_day)

    async def fake_post(model, payload, cfg):
        return _ok_response(model)

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    result = await cloud.cloud_generate(
        "conversation", "sys", [{"role": "user", "content": "hey"}], config
    )
    assert result == "conv-model"


async def test_system_role_messages_are_folded_into_the_system_instruction(tmp_path, monkeypatch):
    config = _config(tmp_path)
    payloads = []

    async def fake_post(model, payload, cfg):
        payloads.append(payload)
        return _ok_response("ok")

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    await cloud.cloud_generate(
        "conversation",
        "base persona",
        [{"role": "system", "content": "Mario often skips X."}, {"role": "user", "content": "hey"}],
        config,
    )

    payload = payloads[0]
    assert "Mario often skips X." in payload["systemInstruction"]["parts"][0]["text"]
    assert all(
        "Mario often skips X." not in p["text"] for c in payload["contents"] for p in c["parts"]
    )
    assert [c["role"] for c in payload["contents"]] == ["user"]


async def test_cloud_chat_puts_music_taste_in_the_system_instruction(tmp_path, monkeypatch):
    import felt_core.brain as brain

    config = _config(tmp_path)
    seen = {}

    async def fake_cloud_generate(purpose, system, messages, cfg, **kwargs):
        seen["system"] = system
        seen["messages"] = messages
        return "hi"

    monkeypatch.setattr(brain, "cloud_generate", fake_cloud_generate)
    monkeypatch.setattr(
        brain,
        "_music_taste_context",
        lambda cfg: [{"role": "system", "content": "Mario often skips X."}],
    )

    await brain._cloud_chat("hey", [], config)

    assert "Mario often skips X." in seen["system"]
    assert all(m["role"] != "system" for m in seen["messages"])
