"""Real network calls, recorded once via VCR (see tests/conftest.py) — the
token-refresh + read paths (search_track, list_devices) against the real
Spotify Web API. play_track itself is deliberately NOT covered here: it's
a real, immediately-audible action on Mario's actual device. Live-verified
once by hand instead (and trivially reversible — pause — unlike a calendar
event, so no cleanup step needed beyond that)."""

import pytest

from felt_core.spotify_client import list_devices, search_track


@pytest.mark.vcr
async def test_search_track_finds_a_real_track(spotify_config):
    import httpx

    async with httpx.AsyncClient(timeout=20) as client:
        track = await search_track(client, spotify_config, "Bohemian Rhapsody Queen")

    assert track is not None
    assert "uri" in track
    assert "Queen" in [a["name"] for a in track["artists"]]


@pytest.mark.vcr
async def test_list_devices_returns_a_list(spotify_config):
    import httpx

    async with httpx.AsyncClient(timeout=20) as client:
        devices = await list_devices(client, spotify_config)

    assert isinstance(devices, list)
