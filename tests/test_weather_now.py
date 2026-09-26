"""WP-21: GET /v1/weather/now - the precompute's condition for desktop props."""

import time

from fastapi.testclient import TestClient

from felt_core import api, brain


def test_weather_now_reports_the_cached_condition(monkeypatch) -> None:
    monkeypatch.setattr(
        brain,
        "_weather_precompute",
        {
            "line": "Rainy, 14 degrees",
            "animation_tag": "reaction/weather/rain",
            "computed_at": time.monotonic() - 30,
        },
    )
    body = TestClient(api.app).get("/v1/weather/now").json()
    assert body["condition"] == "rain"
    assert body["animation_tag"] == "reaction/weather/rain"
    assert 29 <= body["age_s"] <= 31
    assert "line" not in body


def test_weather_now_without_a_fresh_precompute(monkeypatch) -> None:
    monkeypatch.setattr(brain, "_weather_precompute", None)
    assert brain.current_weather() == {"condition": None, "animation_tag": None, "age_s": None}
    stale = {
        "line": "x",
        "animation_tag": "reaction/weather/sun",
        "computed_at": time.monotonic() - 3600,
    }
    monkeypatch.setattr(brain, "_weather_precompute", stale)
    assert brain.current_weather()["condition"] is None


def test_weather_now_unmapped_code_is_none(monkeypatch) -> None:
    monkeypatch.setattr(
        brain,
        "_weather_precompute",
        {"line": "x", "animation_tag": None, "computed_at": time.monotonic()},
    )
    assert brain.current_weather()["condition"] is None
