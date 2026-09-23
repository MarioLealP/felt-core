import httpx

import felt_core.spotify_client as spotify_client
from felt_core.spotify_client import format_track_name, get_currently_playing, pick_device

_TRACK = {
    "id": "abc123",
    "name": "Test Song",
    "artists": [{"name": "Test Artist"}],
    "duration_ms": 200000,
}


class _FakeResponse:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


async def _fake_token(client, config):
    return "tok"


def _reset_cache(monkeypatch):
    monkeypatch.setitem(spotify_client._now_playing_cache, "data", "unset")
    monkeypatch.setitem(spotify_client._now_playing_cache, "cached_at", 0.0)


def test_pick_device_prefers_active():
    devices = [
        {"id": "a", "name": "Desktop", "is_active": False},
        {"id": "b", "name": "Phone", "is_active": True},
    ]
    assert pick_device(devices)["id"] == "b"


def test_pick_device_falls_back_to_first_when_none_active():
    devices = [
        {"id": "a", "name": "Desktop", "is_active": False},
        {"id": "b", "name": "Phone", "is_active": False},
    ]
    assert pick_device(devices)["id"] == "a"


def test_pick_device_none_when_no_devices():
    assert pick_device([]) is None


def test_format_track_name_with_multiple_artists():
    track = {"name": "Stayin' Alive", "artists": [{"name": "Bee Gees"}]}
    assert format_track_name(track) == "Stayin' Alive — Bee Gees"


def test_format_track_name_with_no_artist_field():
    track = {"name": "Unknown Track", "artists": []}
    assert format_track_name(track) == "Unknown Track"


# -- get_currently_playing (WP-16) ------------------------------------------


async def test_get_currently_playing_returns_none_on_204(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(spotify_client, "_get_access_token", _fake_token)

    async def fake_get(self, url, headers):
        return _FakeResponse(204)

    monkeypatch.setattr(spotify_client.httpx.AsyncClient, "get", fake_get)

    async with httpx.AsyncClient() as client:
        result = await get_currently_playing(client, None)

    assert result is None


async def test_get_currently_playing_returns_none_when_body_has_no_item(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(spotify_client, "_get_access_token", _fake_token)

    async def fake_get(self, url, headers):
        return _FakeResponse(200, {"item": None})

    monkeypatch.setattr(spotify_client.httpx.AsyncClient, "get", fake_get)

    async with httpx.AsyncClient() as client:
        result = await get_currently_playing(client, None)

    assert result is None


async def test_get_currently_playing_parses_track_and_progress(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(spotify_client, "_get_access_token", _fake_token)

    async def fake_get(self, url, headers):
        return _FakeResponse(200, {"item": _TRACK, "progress_ms": 50000, "is_playing": True})

    monkeypatch.setattr(spotify_client.httpx.AsyncClient, "get", fake_get)

    async with httpx.AsyncClient() as client:
        result = await get_currently_playing(client, None)

    assert result["track"]["id"] == "abc123"
    assert result["progress_ms"] == 50000
    assert result["duration_ms"] == 200000
    assert result["is_playing"] is True


async def test_get_currently_playing_caches_within_ttl(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(spotify_client, "_get_access_token", _fake_token)
    clock = [1000.0]
    monkeypatch.setattr(spotify_client.time, "monotonic", lambda: clock[0])
    calls = []

    async def fake_get(self, url, headers):
        calls.append(1)
        return _FakeResponse(200, {"item": _TRACK, "progress_ms": 1000, "is_playing": True})

    monkeypatch.setattr(spotify_client.httpx.AsyncClient, "get", fake_get)

    async with httpx.AsyncClient() as client:
        await get_currently_playing(client, None)
        clock[0] += 5  # well within the 10s TTL
        await get_currently_playing(client, None)

    assert len(calls) == 1


async def test_get_currently_playing_refetches_after_ttl(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(spotify_client, "_get_access_token", _fake_token)
    clock = [1000.0]
    monkeypatch.setattr(spotify_client.time, "monotonic", lambda: clock[0])
    calls = []

    async def fake_get(self, url, headers):
        calls.append(1)
        return _FakeResponse(200, {"item": _TRACK, "progress_ms": 1000, "is_playing": True})

    monkeypatch.setattr(spotify_client.httpx.AsyncClient, "get", fake_get)

    async with httpx.AsyncClient() as client:
        await get_currently_playing(client, None)
        clock[0] += 11
        await get_currently_playing(client, None)

    assert len(calls) == 2
