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

import asyncio
import base64
import re
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
# start_track's own skips (uri -> monotonic time) and the songs it started:
# neither is Mario's doing, so taste history and the DJ don't learn from them
_felt_skipped: dict[str, float] = {}
_felt_started: dict[str, float] = {}
_FELT_MARK_S = 900.0
_MAX_SKIPS = 12
_SKIP_SETTLE_S = 0.3
_SKIP_SETTLE_TRIES = 12

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


# "voices in my head from nijuugo*a" - the "from" sent Spotify's search to
# a VALORANT track (live 2026-10-18); "<title> by/from <artist>" searches
# with fields first, and the hit has to look like what he asked for
_BY = re.compile(r"^(?P<title>.+?)\s+(?:by|from)\s+(?P<artist>.+)$", re.IGNORECASE)
_FILLER = re.compile(r"^(?:some|a\s+bit\s+of|the\s+song)\s+", re.IGNORECASE)


async def search_track(client: httpx.AsyncClient, config: FeltConfig, query: str) -> dict | None:
    query = _FILLER.sub("", query.strip())
    m = _BY.match(query)
    tries = [query]
    if m:
        tries = [
            f'track:"{m["title"]}" artist:"{m["artist"]}"',
            f"{m['title']} {m['artist']}",
            query,
        ]
    # no title + artist match: the plain search's top hit (ranked by
    # popularity - Eve's "Kaikai Kitan", not the field search's cover)
    fallback = strict = None
    for q in tries:
        # plain: the top hit is the answer; by/from: a few to check against
        data = await _get(client, config, "/search", q=q, type="track", limit=5 if m else 1)
        items = data.get("tracks", {}).get("items", []) if isinstance(data, dict) else []
        if not items:
            continue
        if m is None:
            return items[0]
        if q == tries[0]:
            strict = items[0]
        else:
            fallback = fallback or items[0]
        hit = next((t for t in items if _looks_like(t, m["title"], m["artist"])), None)
        if hit is not None:
            return hit
    return fallback or strict


def _looks_like(track: dict, title: str, artist: str) -> bool:
    """Pure: the title is in the track's name and the artist among its artists."""
    want_t, want_a = norm_name(title), norm_name(artist)
    if not want_t or want_t not in norm_name(track.get("name", "")):
        return False
    names = [norm_name(a.get("name", "")) for a in track.get("artists", [])]
    return any(n and (want_a in n or n in want_a) for n in names)


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


async def start_track(
    client: httpx.AsyncClient, config: FeltConfig, uri: str, name: str | None = None
) -> None:
    """Plays `uri` now: queued, then skipped to. Not PUT /me/player/play with
    uris - on Mario's Linux desktop client that's accepted (204) and loads
    nothing, and while playing it stops the music (2026-10-18). Queue + next
    works while playing or paused (and resumes); a freshly started client
    with nothing played yet has no active device (404) -> NoActivePlayer.
    The queued song lands behind anything already queued (DJ picks, his own
    adds), and Spotify's queue list lags a fresh add - so it skips one at a
    time, each time waiting for the change to show, until the song is on
    (`name` too: Spotify can relink a track to another uri)."""
    await add_to_queue(client, config, uri)
    _felt_started[uri] = time.monotonic()
    playing = await _playing(client, config)
    for _ in range(_MAX_SKIPS):
        if _is(playing, uri, name):
            return
        if playing is not None:
            _felt_skipped[playing["uri"]] = time.monotonic()
        await next_track(client, config)
        changed = await _settled(client, config, playing)
        if changed is None:  # the skip never showed - stop rather than overshoot
            return
        playing = changed


def _is(track: dict | None, uri: str, name: str | None) -> bool:
    if track is None:
        return False
    return track.get("uri") == uri or (
        name is not None and norm_name(track.get("name", "")) == norm_name(name)
    )


async def _playing(client: httpx.AsyncClient, config: FeltConfig) -> dict | None:
    """The track on right now, uncached."""
    token = await _get_access_token(client, config)
    response = await client.get(
        f"{_API_BASE}/me/player/currently-playing", headers={"Authorization": f"Bearer {token}"}
    )
    if response.status_code == 204:
        return None
    response.raise_for_status()
    return response.json().get("item")


async def _settled(
    client: httpx.AsyncClient, config: FeltConfig, before: dict | None
) -> dict | None:
    """The track once it differs from `before`; None if it never does."""
    old = (before or {}).get("uri")
    for _ in range(_SKIP_SETTLE_TRIES):
        await asyncio.sleep(_SKIP_SETTLE_S)
        now = await _playing(client, config)
        if now is not None and now.get("uri") != old:
            return now
    return None


def felt_skipped(uri: str | None) -> bool:
    """Did start_track skip past `uri` lately (not Mario)?"""
    return uri is not None and time.monotonic() - _felt_skipped.get(uri, -1e9) < _FELT_MARK_S


def felt_started(uri: str | None) -> bool:
    """Did start_track put `uri` on lately (on Mario's ask)?"""
    return uri is not None and time.monotonic() - _felt_started.get(uri, -1e9) < _FELT_MARK_S


async def add_to_queue(client: httpx.AsyncClient, config: FeltConfig, uri: str) -> None:
    """queue_track, with no active device (404) as NoActivePlayer."""
    try:
        await queue_track(client, config, uri)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise NoActivePlayer from e
        raise


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
