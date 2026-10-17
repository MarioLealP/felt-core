"""Spotify — Felt's own dedicated OAuth client (2026-09-14), for
play_music (search + start new playback by name) and, since WP-48
(2026-10-17), media_control's transport (pause/resume/skip/back/volume)
and "save this song": the old MPRIS path went through felt-desktop-agent,
retired in WP-62 M2b. A plain server-side API integration - the Spotify
Web API is public internet, not a local-network call.

Access tokens are refreshed on demand and cached in-process (module-level,
single account) via the shared oauth_token.refresh_access_token helper
(WP-40) - same pattern google_calendar.py uses.
"""

import base64
import time

import httpx

from felt_core.config import FeltConfig
from felt_core.oauth_token import refresh_access_token

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_API_BASE = "https://api.spotify.com/v1"

_token_cache: dict = {"access_token": None, "expires_at": 0.0}

# WP-16: currently-playing is polled a lot (the now_playing skill, the
# /v1/music/now endpoint felt-tray hits every ~15s, and music_taste_loop's
# own poll) - a short cache keeps that from turning into a Spotify API call
# per request. Same module-level pattern as _token_cache above.
_now_playing_cache: dict = {"data": "unset", "cached_at": 0.0}
_NOW_PLAYING_CACHE_TTL_S = 10.0


async def _get_access_token(client: httpx.AsyncClient, config: FeltConfig) -> str:
    basic = base64.b64encode(
        f"{config.spotify_client_id}:{config.spotify_client_secret}".encode()
    ).decode()
    return await refresh_access_token(
        client,
        _token_cache,
        _TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": config.spotify_refresh_token},
        headers={"Authorization": f"Basic {basic}"},
    )


async def search_track(client: httpx.AsyncClient, config: FeltConfig, query: str) -> dict | None:
    token = await _get_access_token(client, config)
    response = await client.get(
        f"{_API_BASE}/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": query, "type": "track", "limit": 1},
    )
    response.raise_for_status()
    items = response.json().get("tracks", {}).get("items", [])
    return items[0] if items else None


async def get_currently_playing(client: httpx.AsyncClient, config: FeltConfig) -> dict | None:
    """WP-16 Part 2: `{track, progress_ms, duration_ms, is_playing}` or
    None when nothing's loaded (204 No Content, or no `item` in the body -
    both happen, e.g. an empty queue after the last track ends). `track`
    is the raw Spotify track object (same shape search_track returns), so
    callers can reuse format_track_name directly instead of re-flattening
    it themselves."""
    now = time.monotonic()
    if (
        _now_playing_cache["data"] != "unset"
        and now < _now_playing_cache["cached_at"] + _NOW_PLAYING_CACHE_TTL_S
    ):
        return _now_playing_cache["data"]

    token = await _get_access_token(client, config)
    response = await client.get(
        f"{_API_BASE}/me/player/currently-playing", headers={"Authorization": f"Bearer {token}"}
    )
    if response.status_code == 204:
        result = None
    else:
        response.raise_for_status()
        body = response.json()
        item = body.get("item")
        result = (
            None
            if item is None
            else {
                "track": item,
                "progress_ms": body.get("progress_ms") or 0,
                "duration_ms": item.get("duration_ms") or 0,
                "is_playing": bool(body.get("is_playing")),
            }
        )

    _now_playing_cache["data"] = result
    _now_playing_cache["cached_at"] = now
    return result


async def list_devices(client: httpx.AsyncClient, config: FeltConfig) -> list[dict]:
    token = await _get_access_token(client, config)
    response = await client.get(
        f"{_API_BASE}/me/player/devices", headers={"Authorization": f"Bearer {token}"}
    )
    response.raise_for_status()
    return response.json().get("devices", [])


async def play_track(
    client: httpx.AsyncClient, config: FeltConfig, track_uri: str, device_id: str
) -> None:
    token = await _get_access_token(client, config)
    response = await client.put(
        f"{_API_BASE}/me/player/play",
        headers={"Authorization": f"Bearer {token}"},
        params={"device_id": device_id},
        json={"uris": [track_uri]},
    )
    response.raise_for_status()


async def _player_call(
    client: httpx.AsyncClient, config: FeltConfig, method: str, path: str, **kwargs
) -> None:
    """One player/library change; the now-playing cache is dropped so the
    next read sees it."""
    token = await _get_access_token(client, config)
    response = await client.request(
        method, f"{_API_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs
    )
    response.raise_for_status()
    _now_playing_cache["data"] = "unset"


async def get_player(client: httpx.AsyncClient, config: FeltConfig) -> dict | None:
    """The playback state (`is_playing`, `device.volume_percent`, ...), or
    None when no device is active (204)."""
    token = await _get_access_token(client, config)
    response = await client.get(
        f"{_API_BASE}/me/player", headers={"Authorization": f"Bearer {token}"}
    )
    if response.status_code == 204:
        return None
    response.raise_for_status()
    return response.json()


async def pause(client: httpx.AsyncClient, config: FeltConfig) -> None:
    await _player_call(client, config, "PUT", "/me/player/pause")


async def resume(client: httpx.AsyncClient, config: FeltConfig) -> None:
    await _player_call(client, config, "PUT", "/me/player/play")


async def next_track(client: httpx.AsyncClient, config: FeltConfig) -> None:
    await _player_call(client, config, "POST", "/me/player/next")


async def previous_track(client: httpx.AsyncClient, config: FeltConfig) -> None:
    await _player_call(client, config, "POST", "/me/player/previous")


async def set_volume(client: httpx.AsyncClient, config: FeltConfig, percent: int) -> None:
    percent = max(0, min(100, percent))
    await _player_call(
        client, config, "PUT", "/me/player/volume", params={"volume_percent": percent}
    )


async def save_to_library(client: httpx.AsyncClient, config: FeltConfig, uri: str) -> None:
    """The generic library endpoint (Spotify's February 2026 change replaced
    PUT /me/tracks); scope user-library-modify."""
    await _player_call(client, config, "PUT", "/me/library", params={"uris": uri})


def pick_device(devices: list[dict]) -> dict | None:
    """Pure: prefer whichever device Spotify already reports as active
    (same "prefer Playing over paused" precedent as media_control's own
    MPRIS player selection), otherwise just the first available one."""
    if not devices:
        return None
    return next((d for d in devices if d.get("is_active")), devices[0])


def format_track_name(track: dict) -> str:
    """Pure: 'Song — Artist' for a Spotify track object."""
    artists = ", ".join(a["name"] for a in track.get("artists", []))
    return f"{track['name']} — {artists}" if artists else track["name"]
