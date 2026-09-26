"""Start new music by name ("play Bohemian Rhapsody") via the Spotify Web
API — the counterpart media_control's own description explicitly excludes
("NOT for starting new music/videos by name"). Targets whichever of Mario's
devices Spotify reports as active, falling back to the first available one
if none is — Spotify's Connect model requires at least one device with the
app open; there's no way to "wake" a fully closed client remotely.
"""

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult
from felt_core.spotify_client import (
    format_track_name,
    list_devices,
    pick_device,
    play_track,
    search_track,
)

name = "play_music"
description = (
    "Starts playing a specific song or artist by name on Spotify, e.g. 'play Bohemian "
    "Rhapsody' or 'play some Daft Punk'. For transport control on whatever's already "
    "playing (pause, skip, volume), use media_control instead."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The song and/or artist to play"},
    },
    "required": ["query"],
}


async def handle(params, ctx):
    config = ctx.config
    if not config.spotify_refresh_token:
        return SkillResult(text="Spotify isn't connected yet.")

    query = get_single_param(params, "query", "song", "track")
    query = str(query or ctx.original_message).strip()
    if not query:
        return SkillResult(text="What should I play?")

    async with httpx.AsyncClient(timeout=20) as client:
        track = await search_track(client, config, query)
        if track is None:
            return SkillResult(text=f"Couldn't find '{query}' on Spotify.")

        devices = await list_devices(client, config)
        device = pick_device(devices)
        if device is None:
            return SkillResult(
                text="No Spotify device is open right now — open Spotify somewhere first."
            )

        await play_track(client, config, track["uri"], device["id"])

    return SkillResult(text=f"Playing {format_track_name(track)} on {device['name']}.")
