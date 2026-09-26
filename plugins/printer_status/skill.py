"""Printer status — multiple providers behind one skill, same pattern as
media_control's Spotify/YouTube split (assistant-skills.md, 2026-09-11:
Mario has three actual printers, each gets its own provider).

- **Ender 3, via OctoPrint** — vendor-agnostic REST API, blocked on an API
  key (not yet provided).
- **HP ENVY 6000, via plain IPP** (Get-Printer-Attributes) — no API key
  needed at all; confirmed reachable from the server, unauthenticated,
  live 2026-09-12. Ink levels come back as a standard IPP attribute this
  model happens to support, not something HP-specific hand-rolled here.
- Elegoo resin printer not yet added — no local network API found for it
  on the LAN (checked mDNS/ARP, 2026-09-12); Home Assistant integration
  remains the fallback per assistant-skills.md, not attempted here.
"""

import httpx
from pyipp import IPP
from pyipp.exceptions import IPPError

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "printer_status"
description = (
    "Gets the current status of a printer — the 3D printer (Ender 3) or the HP document printer."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "printer": {
            "type": "string",
            "description": (
                "Which printer, e.g. 'ender', '3d printer', 'hp', 'document printer' — "
                "optional if only one is configured"
            ),
        }
    },
}


async def _octoprint_status(config) -> str | None:
    if not config.octoprint_url or not config.octoprint_api_key:
        return None
    headers = {"X-Api-Key": config.octoprint_api_key}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        try:
            printer = await client.get(f"{config.octoprint_url}/api/printer")
            job = await client.get(f"{config.octoprint_url}/api/job")
        except httpx.HTTPError:
            return "Couldn't reach the Ender 3 right now."

    if printer.status_code == 409:
        return "The Ender 3 is not currently connected/operational."
    if printer.status_code != 200 or job.status_code != 200:
        return "Couldn't get the Ender 3's status right now."

    printer_data = printer.json()
    job_data = job.json()
    state = job_data.get("state", "Unknown")
    temps = printer_data.get("temperature", {})
    tool0 = temps.get("tool0", {})
    bed = temps.get("bed", {})

    parts = [f"Ender 3 — {state}"]
    if tool0:
        parts.append(f"nozzle {tool0.get('actual', '?')}°C/{tool0.get('target', '?')}°C")
    if bed:
        parts.append(f"bed {bed.get('actual', '?')}°C/{bed.get('target', '?')}°C")
    completion = job_data.get("progress", {}).get("completion")
    if completion is not None:
        parts.append(f"{completion:.1f}% done")
    return ", ".join(parts)


async def _hp_status(config) -> str | None:
    if not config.hp_printer_host:
        return None
    try:
        async with IPP(config.hp_printer_host, port=631, base_path="/ipp/print") as ipp:
            printer = await ipp.printer()
    except IPPError:
        return "Couldn't reach the HP printer right now."

    parts = [f"HP printer — {printer.state.printer_state}"]
    for marker in printer.markers:
        parts.append(f"{marker.name} {marker.level}%")
    return ", ".join(parts)


async def handle(params, ctx):
    config = ctx.config
    which = str(get_single_param(params, "printer", "name") or "").lower()

    wants_ender = "ender" in which or "3d" in which
    wants_hp = "hp" in which or "document" in which or "paper" in which
    if not wants_ender and not wants_hp:
        # No specific printer named — check whatever's actually configured,
        # not "neither" just because the word "printer" alone matches no
        # provider keyword.
        wants_ender = wants_hp = True

    results = []
    if wants_ender:
        result = await _octoprint_status(config)
        if result:
            results.append(result)
    if wants_hp:
        result = await _hp_status(config)
        if result:
            results.append(result)

    if not results:
        return SkillResult(text="No printer matching that is configured.")
    return SkillResult(text=" | ".join(results))
