"""2026-09-24 audit fixes for WP-06: the weather cache's split TTL (20 min
for current conditions, 3 h for a daily forecast) and the skill_phrasing
thinkingLevel that stops Gemma spending its whole output budget thinking."""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import felt_core.cloud as cloud
from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

_SKILL_PATH = Path(__file__).resolve().parent.parent / "plugins" / "get_weather" / "skill.py"


def _load_skill():
    spec = importlib.util.spec_from_file_location("get_weather_ttl_test", _SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_data(temp: float):
    today = date(2026, 9, 24)
    days = [(today + timedelta(days=i)).isoformat() for i in range(3)]
    return {
        "current": {"temperature_2m": temp, "weather_code": 0, "wind_speed_10m": 5.0},
        "daily": {
            "time": days,
            "temperature_2m_max": [temp] * 3,
            "temperature_2m_min": [temp] * 3,
            "weather_code": [0] * 3,
            "precipitation_probability_max": [0] * 3,
        },
    }


def _setup(monkeypatch, tmp_path):
    skill = _load_skill()
    calls = []
    clock = [1000.0]

    async def fake_get(self, url, params):
        calls.append(params)
        return _FakeResponse(_fake_data(20.0 + len(calls)))

    monkeypatch.setattr(skill.httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(skill.time, "monotonic", lambda: clock[0])
    config = FeltConfig(db_path=str(tmp_path / "t.db"), home_latitude=38.7, home_longitude=-9.1)
    ctx = ExecutionContext(config=config, original_message="weather?")
    return skill, calls, clock, ctx


async def test_current_conditions_refetch_after_20_minutes(monkeypatch, tmp_path):
    skill, calls, clock, ctx = _setup(monkeypatch, tmp_path)

    await skill.handle({"day": "now"}, ctx)
    clock[0] += 21 * 60
    await skill.handle({"day": "now"}, ctx)

    assert len(calls) == 2


async def test_daily_forecast_accepts_data_up_to_3_hours_old(monkeypatch, tmp_path):
    skill, calls, clock, ctx = _setup(monkeypatch, tmp_path)

    await skill.handle({"day": "now"}, ctx)
    clock[0] += 2 * 60 * 60  # 2 h later
    await skill.handle({"day": "2026-09-26"}, ctx)

    assert len(calls) == 1
    clock[0] += 61 * 60  # now 3 h 1 min old
    await skill.handle({"day": "2026-09-26"}, ctx)
    assert len(calls) == 2


async def test_todays_forecast_still_needs_current_freshness(monkeypatch, tmp_path):
    skill, calls, clock, ctx = _setup(monkeypatch, tmp_path)

    await skill.handle({"day": "now"}, ctx)
    clock[0] += 30 * 60
    result = await skill.handle({"day": "2026-09-24"}, ctx)

    assert len(calls) == 2  # "Right now: ..." prefix needs fresh current data
    assert result.text.startswith("Right now: 22.0")


# -- thinkingLevel on skill_phrasing ------------------------------------


def _cfg(tmp_path):
    return FeltConfig(
        db_path=str(tmp_path / "felt.db"),
        gemini_api_key="fake-key",
        cloud_model_conversation="conv-model",
        cloud_model_overflow="overflow-model",
        cloud_model_volume="volume-model",
    )


def setup_function() -> None:
    cloud._no_system_instruction.clear()
    cloud._no_thinking_config.clear()
    cloud._recent_calls.clear()
    cloud._recent_tokens.clear()
    cloud._exhausted_until.clear()


async def test_skill_phrasing_asks_for_minimal_thinking(tmp_path, monkeypatch):
    payloads = []

    async def fake_post(model, payload, cfg):
        payloads.append(payload)
        return 200, {"candidates": [{"content": {"parts": [{"text": "sunny"}]}}]}

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    await cloud.cloud_generate("skill_phrasing", "sys", [{"role": "user", "content": "w?"}], _cfg(tmp_path), max_tokens=100)
    await cloud.cloud_generate("conversation", "sys", [{"role": "user", "content": "hi"}], _cfg(tmp_path))

    assert payloads[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert "thinkingConfig" not in payloads[1].get("generationConfig", {})


async def test_thinking_config_rejection_retries_without_it_and_is_remembered(tmp_path, monkeypatch):
    payloads = []

    async def fake_post(model, payload, cfg):
        payloads.append(payload)
        if "thinkingConfig" in payload.get("generationConfig", {}):
            return 400, {"error": {"message": "Thinking level is not supported for this model."}}
        return 200, {"candidates": [{"content": {"parts": [{"text": "sunny"}]}}]}

    monkeypatch.setattr(cloud, "_post_generate_content", fake_post)

    first = await cloud.cloud_generate("skill_phrasing", "sys", [{"role": "user", "content": "w?"}], _cfg(tmp_path))
    second = await cloud.cloud_generate("skill_phrasing", "sys", [{"role": "user", "content": "w?"}], _cfg(tmp_path))

    assert first == second == "sunny"
    assert len(payloads) == 3  # rejected once, retried, then skipped straight to the plain form
