"""WP-48: media_control drives Spotify's Web API (the MPRIS path is gone)."""

import importlib.util
from pathlib import Path

import httpx

from felt_core import spotify_client
from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

_spec = importlib.util.spec_from_file_location(
    "media_control_skill", Path(__file__).parents[1] / "plugins/media_control/skill.py"
)
assert _spec is not None and _spec.loader is not None
skill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill)

_REAL_CLIENT = httpx.AsyncClient
TRACK = {"name": "Midnight City", "uri": "spotify:track:abc", "artists": [{"name": "M83"}]}


def _mock(monkeypatch, calls, playing=True, volume=40, player=True):
    async def token(client, config):
        return "tok"

    monkeypatch.setattr(spotify_client, "_get_access_token", token)
    spotify_client._now_playing_cache["data"] = "unset"

    def handler(request):
        path, method = request.url.path, request.method
        if path == "/v1/me/player" and method == "GET":
            if not player:
                return httpx.Response(204)
            return httpx.Response(
                200,
                json={"is_playing": playing, "item": TRACK, "device": {"volume_percent": volume}},
            )
        if path == "/v1/me/player/currently-playing":
            return httpx.Response(200, json={"item": TRACK, "is_playing": True, "progress_ms": 1})
        calls.append((method, path, dict(request.url.params)))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        skill.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=transport, **kw)
    )


async def _do(monkeypatch, params, **state):
    calls = []
    _mock(monkeypatch, calls, **state)
    ctx = ExecutionContext(config=FeltConfig(spotify_refresh_token="r"))
    return (await skill.handle(params, ctx)).text, calls


async def test_skip_says_what_plays_next(monkeypatch):
    text, calls = await _do(monkeypatch, {"action": "next"})
    assert calls == [("POST", "/v1/me/player/next", {})]
    assert text == "Skipped. Now playing Midnight City — M83."


async def test_save_uses_the_library_endpoint(monkeypatch):
    text, calls = await _do(monkeypatch, {"action": "save"})
    assert calls == [("PUT", "/v1/me/library", {"uris": "spotify:track:abc"})]
    assert text == "Saved Midnight City — M83 to your library."


async def test_louder_is_relative(monkeypatch):
    text, calls = await _do(monkeypatch, {"action": "louder"}, volume=95)
    assert calls == [("PUT", "/v1/me/player/volume", {"volume_percent": "100"})]
    assert text == "Volume 100%."


async def test_volume_takes_a_fraction_or_percent(monkeypatch):
    _, calls = await _do(monkeypatch, {"action": "volume", "level": 0.3})
    assert calls == [("PUT", "/v1/me/player/volume", {"volume_percent": "30"})]
    _, calls = await _do(monkeypatch, {"action": "volume", "level": 55})
    assert calls == [("PUT", "/v1/me/player/volume", {"volume_percent": "55"})]


async def test_pause_when_paused_does_nothing(monkeypatch):
    text, calls = await _do(monkeypatch, {"action": "pause"}, playing=False)
    assert calls == [] and text == "It's already paused."


async def test_play_pause_toggles(monkeypatch):
    _, calls = await _do(monkeypatch, {"action": "play_pause"}, playing=True)
    assert calls == [("PUT", "/v1/me/player/pause", {})]
    _, calls = await _do(monkeypatch, {"action": "play_pause"}, playing=False)
    assert calls == [("PUT", "/v1/me/player/play", {})]


async def test_no_active_player(monkeypatch):
    text, calls = await _do(monkeypatch, {"action": "next"}, player=False)
    assert calls == [] and text == "Nothing's playing on Spotify right now."


async def test_restart_seeks_to_zero(monkeypatch):
    text, calls = await _do(monkeypatch, {"action": "restart"})
    assert calls == [("PUT", "/v1/me/player/seek", {"position_ms": "0"})]
    assert text == "From the top."
