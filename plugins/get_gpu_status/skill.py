"""GPU/VRAM headroom of the Nitro5 AI node — the actual capability lives
on felt-voice, which already runs there as a systemd service, via its
`/v1/gpu-status` endpoint.
"""

import httpx

from felt_core.plugin import SkillResult

name = "get_gpu_status"
description = "GPU load, VRAM and temperature of the Nitro5 AI node."
parameters_schema = {"type": "object", "properties": {}}


async def handle(params, ctx):
    config = ctx.config
    if not config.voice_url:
        return SkillResult(text="GPU status check isn't configured (no voice service URL set).")

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.get(f"{config.voice_url}/v1/gpu-status")
        except httpx.HTTPError:
            return SkillResult(text="Couldn't reach the Nitro5 right now.")

    if response.status_code != 200:
        return SkillResult(text="Couldn't read GPU status from the Nitro5 right now.")

    data = response.json()
    if not data["available"]:
        return SkillResult(text="No GPU detected on the Nitro5.")

    return SkillResult(
        text=(
            f"{data['name']}: {data['utilization_percent']}% utilization, "
            f"{data['memory_used_mb']}/{data['memory_total_mb']} MB VRAM used, "
            f"{data['temperature_c']}°C"
        )
    )
