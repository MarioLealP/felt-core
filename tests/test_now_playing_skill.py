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


_TRACK = {"id": "t1", "name": "Song", "artists": [{"name": "Artist"}], "album": {"name": "Album"}}


async def _fake_now_playing(client, cfg):
    return {"track": _TRACK, "progress_ms": 5000, "duration_ms": 200000, "is_playing": True}


async def test_names_the_current_track(monkeypatch):
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token="dummy-token")
    ctx = ExecutionContext(config=config)

    async def fake_get_lyrics(
        client, cache_dir, track_id, track_name, artist_name, album_name, duration_s, **kwargs
    ):
        return None

    monkeypatch.setattr(skill, "get_currently_playing", _fake_now_playing)
    monkeypatch.setattr(skill, "get_lyrics", fake_get_lyrics)

    result = await skill.handle({}, ctx)

    assert "Song" in result.text
    assert "Artist" in result.text


async def test_quotes_the_current_lyric_line_when_available(monkeypatch):
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token="dummy-token")
    ctx = ExecutionContext(config=config)

    async def fake_get_lyrics(
        client, cache_dir, track_id, track_name, artist_name, album_name, duration_s, **kwargs
    ):
        return {
            "synced": [{"time_ms": 1000, "text": "A quotable line"}],
            "plain": "A quotable line",
        }

    monkeypatch.setattr(skill, "get_currently_playing", _fake_now_playing)
    monkeypatch.setattr(skill, "get_lyrics", fake_get_lyrics)

    result = await skill.handle({}, ctx)

    assert "A quotable line" in result.text


async def test_omits_the_quote_when_no_lyrics_found(monkeypatch):
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token="dummy-token")
    ctx = ExecutionContext(config=config)

    async def fake_get_lyrics(
        client, cache_dir, track_id, track_name, artist_name, album_name, duration_s, **kwargs
    ):
        return None

    monkeypatch.setattr(skill, "get_currently_playing", _fake_now_playing)
    monkeypatch.setattr(skill, "get_lyrics", fake_get_lyrics)

    result = await skill.handle({}, ctx)

    assert "lyric" not in result.text.lower()


async def test_a_lyrics_fetch_error_does_not_break_the_basic_answer(monkeypatch):
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token="dummy-token")
    ctx = ExecutionContext(config=config)

    async def fake_get_lyrics(
        client, cache_dir, track_id, track_name, artist_name, album_name, duration_s, **kwargs
    ):
        raise RuntimeError("boom")

    monkeypatch.setattr(skill, "get_currently_playing", _fake_now_playing)
    monkeypatch.setattr(skill, "get_lyrics", fake_get_lyrics)

    result = await skill.handle({}, ctx)

    assert "Song" in result.text
