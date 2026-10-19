"""Find-my-phone: a loud notification on the phone's alarm stream, via the
Home Assistant app (phone.py, 2026-10-18 - KDE Connect went with the
Windows desktop agent). A real-world side effect, not just information."""

from felt_core.phone import PhoneUnavailable, ring
from felt_core.plugin import SkillResult

name = "find_phone"
description = "Makes Mario's phone ring loudly so he can find it."
parameters_schema = {"type": "object", "properties": {}}


async def handle(params, ctx):
    try:
        await ring(ctx.config)
    except PhoneUnavailable:
        return SkillResult(text="Couldn't reach the phone right now.")
    return SkillResult(text="Ringing your phone.")
