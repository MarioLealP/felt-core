"""Find-my-phone via KDE Connect (rings it). Makes the actual physical
phone buzz/ring — a real-world side effect, not just information, unlike
most of Felt's other skills."""

import httpx

from felt_core.plugin import SkillResult

name = "find_phone"
description = "Makes Mario's phone ring/buzz to help find it, via KDE Connect."
parameters_schema = {"type": "object", "properties": {}}


async def handle(params, ctx):
    config = ctx.config
    if not config.desktop_agent_url:
        return SkillResult(text="Find-my-phone isn't configured (no desktop agent URL set).")

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.post(f"{config.desktop_agent_url}/v1/kdeconnect/ring")
        except httpx.HTTPError:
            return SkillResult(text="Couldn't reach the phone right now.")

    if response.status_code != 200:
        return SkillResult(text="No phone available via KDE Connect right now.")
    return SkillResult(text="Ringing your phone now.")
