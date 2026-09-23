"""Answers "what's this song?" by reading Spotify's currently-playing
endpoint directly (Mario, 2026-09-22: "she can have access to the lyrics
and song name directly from Spotify, I don't care if she knows the name of
the song"). Read-only - no device/transport control, unlike play_music
(starts something new) and media_control (pause/skip/volume on whatever's
already loaded).
"""

import httpx

from felt_core.plugin import SkillResult
from felt_core.spotify_client import format_track_name, get_currently_playing

name = "now_playing"
description = (
    "Answers what song is currently playing on Spotify right now, e.g. 'what's this "
    "song?' or 'what are you listening to'. Read-only - does not start or control playback."
)
parameters_schema = {"type": "object", "properties": {}}


async def handle(params, ctx):
    config = ctx.config
    if not config.spotify_refresh_token:
        return SkillResult(text="Spotify isn't connected.")

    async with httpx.AsyncClient(timeout=20) as client:
        now_playing = await get_currently_playing(client, config)

    if now_playing is None or not now_playing["is_playing"]:
        return SkillResult(text="Nothing's playing right now.")

    return SkillResult(text=f"This is {format_track_name(now_playing['track'])}.")
