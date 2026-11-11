"""What's playing: /v1/music/now and /v1/music/lyrics."""

from pathlib import Path

import httpx2
from fastapi import APIRouter

from felt_core.clients.lyrics_client import current_line, find_chorus, get_lyrics, with_end_times
from felt_core.clients.spotify_client import format_track_name, get_currently_playing
from felt_core.config import load_config

router = APIRouter()


@router.get("/v1/music/now")
async def music_now_endpoint() -> dict:
    """WP-16 Part 2: what's currently playing on Spotify.

    Polled by felt-tray every ~15s to drive unprompted "oh, this one
    again" remarks on a track change, and answerable directly via the
    now_playing skill. `{"is_playing": false}` covers both "nothing
    loaded" and "Spotify not connected" - callers don't need to
    distinguish those.
    """
    config = load_config()
    if not config.spotify_refresh_token:
        return {"is_playing": False}

    async with httpx2.AsyncClient(timeout=20) as client:
        now_playing = await get_currently_playing(client, config)

    if now_playing is None:
        return {"is_playing": False}

    track = now_playing["track"]
    return {
        "is_playing": now_playing["is_playing"],
        "track": track.get("name", ""),
        "artists": [a["name"] for a in track.get("artists", [])],
        "album": track.get("album", {}).get("name", ""),
        # A local file has id: null - its uri still identifies it.
        "track_id": track.get("id") or track.get("uri") or "",
        "display_name": format_track_name(track),
        "progress_ms": now_playing["progress_ms"],
        "duration_ms": now_playing["duration_ms"],
    }


@router.get("/v1/music/lyrics")
async def music_lyrics_endpoint() -> dict:
    """WP-16 Part 3: lyrics for whatever's currently playing, from LRCLIB (Spotify's own API has no lyrics endpoint).

    `current_line`/`chorus` are only populated when LRCLIB returned synced
    (LRC) lyrics - plenty of tracks only have plain text, which is still
    usable for a quoted remark but has no per-line timing to build them
    from.
    """
    config = load_config()
    if not config.spotify_refresh_token:
        return {"found": False}

    async with httpx2.AsyncClient(timeout=20) as client:
        now_playing = await get_currently_playing(client, config)
        if now_playing is None:
            return {"found": False}

        track = now_playing["track"]
        lyrics = await get_lyrics(
            client,
            Path(config.lyrics_cache_dir),
            track.get("id", ""),
            track.get("name", ""),
            ", ".join(a["name"] for a in track.get("artists", [])),
            track.get("album", {}).get("name", ""),
            now_playing["duration_ms"] // 1000,
            user_agent=config.lyrics_user_agent,
        )

    if lyrics is None:
        return {"found": False}

    synced = lyrics["synced"]
    progress_ms = now_playing["progress_ms"]
    # `chorus` keeps its original start_ms/end_ms/text fields (now pointing
    # at the current-or-next chorus occurrence rather than always the
    # first) and adds `windows` (every occurrence, with per-line timings).
    # `lines` is the whole synced lyric with real start/end times, and
    # `progress_ms` the position they were computed against, so the
    # desktop can build a mouth-along timeline from real timings.
    return {
        "found": True,
        "has_synced": bool(synced),
        "current_line": current_line(synced, progress_ms) if synced else None,
        "chorus": find_chorus(synced, progress_ms) if synced else None,
        "lines": with_end_times(synced) if synced else [],
        "progress_ms": progress_ms,
        "plain_excerpt": (lyrics["plain"][:200] if lyrics["plain"] else None),
    }
