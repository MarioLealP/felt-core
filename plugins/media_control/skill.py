"""Spotify transport + "save this song" (WP-48): pause, resume, skip, go
back, volume, and saving what's playing to Mario's library. Through the
Spotify Web API since 2026-10-17 - the old MPRIS path went through
felt-desktop-agent, retired in WP-62 M2b, so this had been dead since.
Starting something new by name is play_music; "what's this song" is
now_playing.
"""

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult
from felt_core.spotify_client import (
    format_track_name,
    get_currently_playing,
    get_player,
    next_track,
    pause,
    previous_track,
    resume,
    save_to_library,
    set_volume,
)

name = "media_control"
description = (
    "Controls the music already playing on Spotify: pause, resume, skip to the next song, "
    "go back, set the volume or turn it up/down, or save the current song to Mario's "
    "library. NOT for starting new music by name (that's play_music)."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": (
                "'pause', 'resume', 'play_pause', 'next', 'previous', 'volume', 'louder', "
                "'quieter' or 'save'"
            ),
        },
        "level": {
            "type": "number",
            "description": "volume 0-100 (or 0.0-1.0), only with the volume action",
        },
    },
}

_STEP = 10  # louder / quieter, in percent


def _operation(action: str) -> str:
    if "save" in action or "like" in action:
        return "save"
    if "next" in action or "skip" in action:
        return "next"
    if "prev" in action or "back" in action:
        return "previous"
    if "louder" in action or "up" in action:
        return "louder"
    if "quiet" in action or "down" in action or "lower" in action:
        return "quieter"
    if "volume" in action:
        return "volume"
    if "resume" in action or "unpause" in action:
        return "resume"
    if action.strip() == "pause" or "stop" in action:
        return "pause"
    return "play_pause"


def _percent(level) -> int | None:
    try:
        value = float(level)
    except (TypeError, ValueError):
        return None
    return round(value * 100) if 0 < value <= 1 and "." in str(level) else round(value)


async def handle(params, ctx):
    config = ctx.config
    if not config.spotify_refresh_token:
        return SkillResult(text="Spotify isn't connected.")
    action = str(
        get_single_param(params, "action", "command", ignore_keys=("level", "volume")) or ""
    ).lower()
    op = _operation(action)

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            player = await get_player(client, config)
            if player is None:
                return SkillResult(text="Nothing's playing on Spotify right now.")
            item = player.get("item") or {}
            if op == "save":
                if not item.get("uri"):
                    return SkillResult(text="Nothing's playing to save.")
                await save_to_library(client, config, item["uri"])
                return SkillResult(text=f"Saved {format_track_name(item)} to your library.")
            if op == "next":
                await next_track(client, config)
                now = await get_currently_playing(client, config)
                name = format_track_name(now["track"]) if now else "the next song"
                return SkillResult(text=f"Skipped. Now playing {name}.")
            if op == "previous":
                await previous_track(client, config)
                return SkillResult(text="Back to the previous song.")
            volume = (player.get("device") or {}).get("volume_percent")
            if op in ("louder", "quieter", "volume"):
                if op == "volume":
                    target = _percent(
                        get_single_param(params, "level", "volume", ignore_keys=("action",))
                    )
                    if target is None:
                        return SkillResult(text=f"Volume is at {volume}%. To what?")
                else:
                    if volume is None:
                        return SkillResult(text="That player doesn't take volume changes.")
                    target = volume + (_STEP if op == "louder" else -_STEP)
                target = max(0, min(100, target))
                await set_volume(client, config, target)
                return SkillResult(text=f"Volume {target}%.")
            playing = bool(player.get("is_playing"))
            if op == "play_pause":
                op = "pause" if playing else "resume"
            if op == "pause":
                if not playing:
                    return SkillResult(text="It's already paused.")
                await pause(client, config)
                return SkillResult(text="Paused.")
            if playing:
                return SkillResult(text="It's already playing.")
            await resume(client, config)
            return SkillResult(text="Playing again.")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                return SkillResult(
                    text="Spotify refused that (it needs Premium, or the player is restricted)."
                )
            return SkillResult(text="Spotify didn't take that right now.")
        except httpx.HTTPError:
            return SkillResult(text="Couldn't reach Spotify right now.")
