"""Start new music by name ("play Bohemian Rhapsody") via the Spotify Web
API — the counterpart media_control's own description explicitly excludes
("NOT for starting new music/videos by name"). Started as queue + skip
(spotify_client.start_track - the plain play call doesn't work on Mario's
Linux client); a player that hasn't played anything since it opened can't be
woken remotely.

"queue X" / "add X to the queue" / "play X next" only queues it (when=queue,
Mario 2026-10-18: "make it different things, add to queue or play").
"""

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult
from felt_core.spotify_client import (
    NoActivePlayer,
    add_to_queue,
    format_track_name,
    search_track,
    start_track,
)

name = "play_music"
description = (
    "Starts playing a specific song or artist by name on Spotify, e.g. 'play Bohemian "
    "Rhapsody' or 'play some Daft Punk' - or, with when=queue, only adds it to the queue "
    "('queue X', 'add X to the queue', 'play X next'). For transport control on whatever's "
    "already playing (pause, skip, volume), use media_control instead."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The song and/or artist to play"},
        "when": {
            "type": "string",
            "enum": ["now", "queue"],
            "description": "now = play it right away (default); queue = add it to the queue",
        },
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

        queue = params.get("when") == "queue"
        try:
            await (add_to_queue if queue else start_track)(client, config, track["uri"])
        except NoActivePlayer:
            return SkillResult(
                text="Spotify's asleep on your desktop - hit play on anything once, then ask again."
            )
        except httpx.HTTPError:
            return SkillResult(text="Spotify didn't take that right now.")

    return SkillResult(text=f"{'Queued' if queue else 'Playing'} {format_track_name(track)}.")
