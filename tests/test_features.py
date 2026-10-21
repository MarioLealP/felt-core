"""Her feature switches (features.py): defaults, skills, the app routes, and
that a switched-off feature spends no model call."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from felt_core import asides, features, local_llm
from felt_core.api import app
from felt_core.brain import screen_reactions
from felt_core.config import FeltConfig

SSO = {"X-Authentik-Username": "mario"}


def _config(tmp_path) -> FeltConfig:
    return FeltConfig(db_path=str(tmp_path / "felt.db"), local_llm_url="http://felt-llm")


def test_defaults_and_switching(tmp_path):
    db = str(tmp_path / "f.db")
    assert features.enabled(db, "ambient.song_remarks")
    for off in ("ambient.asides", "ambient.marks", "ambient.weather_line"):
        assert not features.enabled(db, off)
    features.set_enabled(db, "ambient.song_remarks", False)
    features.set_enabled(db, "ambient.asides", True)
    assert not features.enabled(db, "ambient.song_remarks")
    assert features.enabled(db, "ambient.asides")
    with pytest.raises(ValueError):
        features.enabled(db, "ambient.nope")


def test_skills_are_on_until_switched_off(tmp_path):
    db = str(tmp_path / "f.db")
    registry = {"web_search": object(), "find_phone": object()}
    assert features.enabled_skills(db, registry) == registry
    features.set_enabled(db, "skill.find_phone", False)
    assert list(features.enabled_skills(db, registry)) == ["web_search"]
    rows = {r["key"]: r["on"] for r in features.listing(db, list(registry))}
    assert rows["skill.find_phone"] is False and rows["skill.web_search"] is True
    assert rows["ambient.marks"] is False


def test_the_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    with TestClient(app) as c:
        assert c.get("/v1/app/features").status_code == 401
        rows = c.get("/v1/app/features", headers=SSO).json()
        assert next(r for r in rows if r["key"] == "ambient.asides")["on"] is False
        assert any(r["key"] == "skill.web_search" for r in rows)
        body = {"key": "ambient.song_remarks", "on": False}
        rows = c.post("/v1/app/features", headers=SSO, json=body).json()
        assert next(r for r in rows if r["key"] == "ambient.song_remarks")["on"] is False
        for bad in ("ambient.nope", "web_search", "skill.nope"):
            r = c.post("/v1/app/features", headers=SSO, json={"key": bad, "on": False})
            assert r.status_code == 422, bad


def test_asides_off_schedules_nothing(tmp_path, monkeypatch):
    config = _config(tmp_path)
    started = []
    monkeypatch.setattr(asides, "_pick_loop", lambda *a: started.append(a))
    asides.schedule_pick(config, asides.APP_SESSION)
    assert started == []


def test_screen_off_makes_no_model_call_but_look_at_this_still_does(tmp_path, monkeypatch):
    config = _config(tmp_path)
    features.set_enabled(config.db_path, "ambient.screen_reactions", False)

    async def no_call(*_a, **_k):
        raise AssertionError("model called")

    monkeypatch.setattr(local_llm, "chat", no_call)
    result = asyncio.run(
        screen_reactions.react_to_screen(b"jpeg", "firefox", "wiki", config, trigger="periodic")
    )
    assert result.reply == ""
    with pytest.raises(AssertionError, match="model called"):
        asyncio.run(
            screen_reactions.react_to_screen(b"jpeg", "firefox", "wiki", config, trigger="manual")
        )
