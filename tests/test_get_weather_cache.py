import importlib.util
from pathlib import Path

_SKILL_PATH = Path(__file__).resolve().parent.parent / "plugins" / "get_weather" / "skill.py"


def _load_skill():
    spec = importlib.util.spec_from_file_location("get_weather_cache_test", _SKILL_PATH)
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
    return {
        "current": {"temperature_2m": temp, "weather_code": 0, "wind_speed_10m": 5.0},
        "daily": {
            "time": ["2026-09-22"],
            "temperature_2m_max": [temp],
            "temperature_2m_min": [temp],
            "weather_code": [0],
            "precipitation_probability_max": [0],
        },
    }


async def test_second_fetch_within_ttl_uses_the_cache(monkeypatch):
    skill = _load_skill()
    calls = []
    clock = [1000.0]

    async def fake_get(self, url, params):
        calls.append(params)
        return _FakeResponse(_fake_data(20.0))

    monkeypatch.setattr(skill.httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(skill.time, "monotonic", lambda: clock[0])

    first = await skill._fetch(38.7, -9.1, "celsius")
    clock[0] += 60  # well within the 20-minute TTL
    second = await skill._fetch(38.7, -9.1, "celsius")

    assert first == second
    assert len(calls) == 1


async def test_fetch_after_ttl_expiry_refetches(monkeypatch):
    skill = _load_skill()
    calls = []
    clock = [1000.0]

    async def fake_get(self, url, params):
        calls.append(params)
        return _FakeResponse(_fake_data(20.0 + len(calls)))

    monkeypatch.setattr(skill.httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(skill.time, "monotonic", lambda: clock[0])

    await skill._fetch(38.7, -9.1, "celsius")
    clock[0] += skill._CACHE_TTL_S + 1
    await skill._fetch(38.7, -9.1, "celsius")

    assert len(calls) == 2


async def test_different_units_are_cached_separately(monkeypatch):
    skill = _load_skill()
    calls = []
    clock = [1000.0]

    async def fake_get(self, url, params):
        calls.append(params)
        return _FakeResponse(_fake_data(20.0))

    monkeypatch.setattr(skill.httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(skill.time, "monotonic", lambda: clock[0])

    await skill._fetch(38.7, -9.1, "celsius")
    await skill._fetch(38.7, -9.1, "fahrenheit")

    assert len(calls) == 2
