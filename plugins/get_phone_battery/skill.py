"""Phone battery via KDE Connect — the actual capability lives on the
desktop (felt-desktop-agent), where kdeconnectd runs, not here."""

import httpx

from felt_core.plugin import SkillResult

name = "get_phone_battery"
description = "Gets the current battery level of Mario's phone, via KDE Connect."
parameters_schema = {"type": "object", "properties": {}}


async def handle(params, ctx):
    config = ctx.config
    if not config.desktop_agent_url:
        return SkillResult(text="Phone battery check isn't configured (no desktop agent URL set).")

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.get(f"{config.desktop_agent_url}/v1/kdeconnect/battery")
        except httpx.HTTPError:
            return SkillResult(text="Couldn't reach the phone right now.")

    if response.status_code != 200:
        return SkillResult(text="No phone available via KDE Connect right now.")

    data = response.json()
    state = "charging" if data["is_charging"] else "not charging"
    return SkillResult(text=f"Phone battery: {data['charge']}% ({state})")
