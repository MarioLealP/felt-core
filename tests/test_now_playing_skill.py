import importlib.util
from pathlib import Path

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

_SKILL_PATH = Path(__file__).resolve().parent.parent / "plugins" / "now_playing" / "skill.py"


def _load_skill():
    spec = importlib.util.spec_from_file_location("now_playing_test", _SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_reports_unconfigured_when_no_refresh_token():
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token=None)
    ctx = ExecutionContext(config=config)

    result = await skill.handle({}, ctx)

    assert "isn't connected" in result.text


async def test_reports_nothing_playing_when_no_track(monkeypatch):
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token="dummy-token")
    ctx = ExecutionContext(config=config)

    async def fake_get_currently_playing(client, cfg):
        return None

    monkeypatch.setattr(skill, "get_currently_playing", fake_get_currently_playing)

    result = await skill.handle({}, ctx)

    assert "nothing" in result.text.lower()


async def test_reports_nothing_playing_when_paused(monkeypatch):
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token="dummy-token")
    ctx = ExecutionContext(config=config)

    async def fake_get_currently_playing(client, cfg):
        return {"track": {"name": "Song", "artists": [{"name": "Artist"}]}, "is_playing": False}

    monkeypatch.setattr(skill, "get_currently_playing", fake_get_currently_playing)

    result = await skill.handle({}, ctx)

    assert "nothing" in result.text.lower()


async def test_names_the_current_track(monkeypatch):
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token="dummy-token")
    ctx = ExecutionContext(config=config)

    async def fake_get_currently_playing(client, cfg):
        return {"track": {"name": "Song", "artists": [{"name": "Artist"}]}, "is_playing": True}

    monkeypatch.setattr(skill, "get_currently_playing", fake_get_currently_playing)

    result = await skill.handle({}, ctx)

    assert "Song" in result.text
    assert "Artist" in result.text
