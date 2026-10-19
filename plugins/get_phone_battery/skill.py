"""Phone battery from the Home Assistant app's sensors (phone.py,
2026-10-18 - KDE Connect went with the Windows desktop agent)."""

from felt_core.phone import PhoneUnavailable, battery
from felt_core.plugin import SkillResult

name = "get_phone_battery"
description = "Mario's phone battery level."
parameters_schema = {"type": "object", "properties": {}}


async def handle(params, ctx):
    try:
        percent, charging = await battery(ctx.config)
    except PhoneUnavailable:
        return SkillResult(text="Couldn't reach the phone right now.")
    state = "charging" if charging else "not charging"
    return SkillResult(text=f"Phone battery: {percent}% ({state})")
