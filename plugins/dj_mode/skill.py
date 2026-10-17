"""WP-48 DJ mode: Felt runs Mario's Spotify queue - her picks from his taste,
about a third new to him, a line as each comes on, learning from his skips.
Also "more like this" / "not this" about the current song."""

from felt_core.dj import control
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "dj_mode"
description = (
    "DJ mode: Felt picks and queues Mario's music on Spotify herself ('be my DJ', 'DJ mode', "
    "'DJ mode off'), and takes his feedback on the current song ('more like this', 'not "
    "this'). For a specific song by name use play_music."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "'start', 'stop', 'more' or 'less'"},
    },
}


async def handle(params, ctx):
    config = ctx.config
    if not config.spotify_refresh_token:
        return SkillResult(text="Spotify isn't connected.")
    action = str(get_single_param(params, "action", "command") or "start").lower()
    if "stop" in action or "off" in action or "end" in action:
        was_on = control.is_on()
        control.stop("Mario asked")
        return SkillResult(text="DJ mode off." if was_on else "DJ mode wasn't on.")
    if "less" in action or "not" in action or "dislike" in action:
        return SkillResult(text=await control.feedback(config, liked=False))
    if "more" in action or "like" in action:
        return SkillResult(text=await control.feedback(config, liked=True))
    return SkillResult(text=await control.start(config))
