"""3D printer status via OctoPrint — deliberately vendor-agnostic.
Talks to OctoPrint's own generic API, not the Ender 3 specifically, so
this keeps working unchanged if the printer's ever replaced (same
philosophy as the media-control skill's MPRIS layer).

Reached container-to-container on the 'authentik' Docker network
(bypassing Caddy/SSO entirely) since OctoPrint has its own separate
API-key auth — no forward-auth cookie dance needed for a backend call.
"""

import httpx

from felt_core.plugin import SkillResult

name = "printer_status"
description = "Gets the current 3D printer (OctoPrint) status — printing, idle, temperatures, job progress."
parameters_schema = {"type": "object", "properties": {}}


async def handle(params, ctx):
    config = ctx.config
    if not config.octoprint_url or not config.octoprint_api_key:
        return SkillResult(text="Printer status isn't configured (no OctoPrint URL/key set).")

    headers = {"X-Api-Key": config.octoprint_api_key}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        try:
            printer = await client.get(f"{config.octoprint_url}/api/printer")
            job = await client.get(f"{config.octoprint_url}/api/job")
        except httpx.HTTPError:
            return SkillResult(text="Couldn't reach the printer right now.")

    if printer.status_code == 409:
        return SkillResult(text="Printer is not currently connected/operational.")
    if printer.status_code != 200 or job.status_code != 200:
        return SkillResult(text="Couldn't get printer status right now.")

    printer_data = printer.json()
    job_data = job.json()
    state = job_data.get("state", "Unknown")
    temps = printer_data.get("temperature", {})
    tool0 = temps.get("tool0", {})
    bed = temps.get("bed", {})

    parts = [f"State: {state}"]
    if tool0:
        parts.append(f"Nozzle: {tool0.get('actual', '?')}°C / {tool0.get('target', '?')}°C")
    if bed:
        parts.append(f"Bed: {bed.get('actual', '?')}°C / {bed.get('target', '?')}°C")

    completion = job_data.get("progress", {}).get("completion")
    if completion is not None:
        parts.append(f"Progress: {completion:.1f}%")

    return SkillResult(text=", ".join(parts))
