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
from typing import Any

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


async def restart_track(client: httpx.AsyncClient, config: FeltConfig) -> None:
    await _player_call(client, config, "PUT", "/me/player/seek", params={"position_ms": 0})


async def set_volume(client: httpx.AsyncClient, config: FeltConfig, percent: int) -> None:
    percent = max(0, min(100, percent))
    await _player_call(
        client, config, "PUT", "/me/player/volume", params={"volume_percent": percent}
    )


async def save_to_library(client: httpx.AsyncClient, config: FeltConfig, uri: str) -> None:
    """The generic library endpoint (Spotify's February 2026 change replaced
    PUT /me/tracks); scope user-library-modify."""
    await _player_call(client, config, "PUT", "/me/library", params={"uris": uri})


async def queue_track(client: httpx.AsyncClient, config: FeltConfig, uri: str) -> None:
    await _player_call(client, config, "POST", "/me/player/queue", params={"uri": uri})


class NoActivePlayer(Exception):
    """Spotify has no awake player to start the song on."""


async def start_track(client: httpx.AsyncClient, config: FeltConfig, uri: str) -> None:
    """Plays `uri` now: queued, then skipped to. Not PUT /me/player/play with
    uris - on Mario's Linux desktop client that's accepted (204) and loads
    nothing, and while playing it stops the music (2026-10-18). Queue + next
    works while playing or paused (and resumes); a freshly started client
    with nothing played yet has no active device (404) -> NoActivePlayer.
    The queued song lands behind anything already queued (DJ picks, his own
    adds), so it skips as far as the queue says, not just once."""
    try:
        await queue_track(client, config, uri)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise NoActivePlayer from e
        raise
    for _ in range(await _skips_to(client, config, uri)):
        await next_track(client, config)


async def _skips_to(client: httpx.AsyncClient, config: FeltConfig, uri: str) -> int:
    """How many nexts reach `uri` in the up-next list; 1 if it isn't listed
    (the list is cut at ~20) or the read fails."""
    try:
        data = await _get(client, config, "/me/player/queue")
    except httpx.HTTPError:
        return 1
    queue = data.get("queue", []) if isinstance(data, dict) else []
    uris = [t.get("uri") for t in queue if isinstance(t, dict)]
    return uris.index(uri) + 1 if uri in uris else 1


async def _get(client: httpx.AsyncClient, config: FeltConfig, path: str, **params) -> Any:
    token = await _get_access_token(client, config)
    response = await client.get(
        f"{_API_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, params=params
    )
    response.raise_for_status()
    return response.json()


async def top_artists(client: httpx.AsyncClient, config: FeltConfig, limit: int = 30) -> list[str]:
    """Mario's top artists over ~6 months (scope user-top-read)."""
    data = await _get(client, config, "/me/top/artists", time_range="medium_term", limit=limit)
    items = data.get("items", []) if isinstance(data, dict) else []
    return [a["name"] for a in items if a.get("name")]


async def library_contains(
    client: httpx.AsyncClient, config: FeltConfig, uris: list[str]
) -> list[bool]:
    """Which of `uris` (max 40) Mario has saved (scope user-library-read)."""
    if not uris:
        return []
    data = await _get(client, config, "/me/library/contains", uris=",".join(uris[:40]))
    return [bool(x) for x in data] if isinstance(data, list) else [False] * len(uris)


def norm_name(text: str) -> str:
    """Lowercase letters and digits only, for comparing names."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


async def artist_tracks(client: httpx.AsyncClient, config: FeltConfig, artist: str) -> list[dict]:
    """Tracks whose main artist is `artist` (by name, Spotify's search order -
    roughly the popular ones first); empty if Spotify has no such artist."""
    data = await _get(client, config, "/search", q=f'artist:"{artist}"', type="track", limit=10)
    want = norm_name(artist)
    items = data.get("tracks", {}).get("items", []) if isinstance(data, dict) else []
    return [
        t for t in items if t.get("artists") and norm_name(t["artists"][0].get("name", "")) == want
    ]


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
