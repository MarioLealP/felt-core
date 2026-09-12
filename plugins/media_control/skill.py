"""Media transport control (play/pause/skip/volume) for whatever's
currently loaded on Mario's desktop — the actual capability lives on
felt-desktop-agent (MPRIS via playerctl), not here.
"""

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "media_control"
description = (
    "Controls or checks whatever's currently playing on Mario's desktop — play, "
    "pause, skip, go back, volume, or what's playing right now. NOT for starting "
    "new music/videos by name (searching for a song), only controlling what's "
    "already loaded."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": "'status' (default, what's playing), 'play_pause', 'next', 'previous', or 'volume'",
        },
        "level": {
            "type": "number",
            "description": "volume level from 0.0 to 1.0, only used with the volume action",
        },
    },
}


async def handle(params, ctx):
    config = ctx.config
    if not config.desktop_agent_url:
        return SkillResult(text="Media control isn't configured (no desktop agent URL set).")

    action = str(get_single_param(params, "action", "command", ignore_keys=("level", "volume")) or "status").lower()

    if "next" in action or "skip" in action:
        operation = "next"
    elif "prev" in action or "back" in action:
        operation = "previous"
    elif "volume" in action:
        operation = "volume"
    elif "status" in action or "playing" in action:
        operation = "status"
    else:
        operation = "play_pause"

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            if operation == "status":
                response = await client.get(f"{config.desktop_agent_url}/v1/media/status")
            elif operation == "volume":
                level = get_single_param(params, "level", "volume", ignore_keys=("action", "command"))
                if level is None:
                    return SkillResult(text="What volume level (0.0 to 1.0)?")
                response = await client.post(f"{config.desktop_agent_url}/v1/media/volume", params={"level": level})
            else:
                response = await client.post(f"{config.desktop_agent_url}/v1/media/{operation.replace('_', '-')}")
        except httpx.HTTPError:
            return SkillResult(text="Couldn't reach the desktop right now.")

    if response.status_code == 503:
        return SkillResult(text="Nothing appears to be playing right now.")
    response.raise_for_status()

    if operation == "status":
        data = response.json()
        if not data["active"]:
            return SkillResult(text="Nothing is currently loaded in any media player.")
        return SkillResult(text=f"{data['status']}: {data['title']} — {data['artist']}")

    return SkillResult(text="Done.")
