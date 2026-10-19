"""Printer status — multiple providers behind one skill, same pattern as
media_control's Spotify/YouTube split (assistant-skills.md, 2026-09-11:
Mario has three actual printers, each gets its own provider).

- **Ender, via Home Assistant** (since 2026-10-19: Klipper's moonraker ->
  MQTT -> HA's `sensor.ender_*`, read with Felt's HA token; felt_core.ender
  also says when a print ends). OctoPrint is gone.
- **HP ENVY 6000, via plain IPP** (Get-Printer-Attributes) — no API key
  needed at all; confirmed reachable from the server, unauthenticated,
  live 2026-09-12. Ink levels come back as a standard IPP attribute this
  model happens to support, not something HP-specific hand-rolled here.
- Elegoo resin printer not yet added — no local network API found for it
  on the LAN (checked mDNS/ARP, 2026-09-12); Home Assistant integration
  remains the fallback per assistant-skills.md, not attempted here.
"""

from pyipp import IPP
from pyipp.exceptions import IPPError

from felt_core import ender
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "printer_status"
description = "Status of the 3D printer (Ender) or the HP printer."
parameters_schema = {
    "type": "object",
    "properties": {"printer": {"type": "string", "description": "'3d printer' or 'hp' (optional)"}},
}


async def _ender_status(config) -> str | None:
    if not (config.ha_url and config.ha_token):
        return None
    try:
        return ender.describe(await ender.snapshot(config))
    except ender.EnderUnavailable:
        return "Couldn't reach Home Assistant for the Ender right now."


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
        result = await _ender_status(config)
        if result:
            results.append(result)
    if wants_hp:
        result = await _hp_status(config)
        if result:
            results.append(result)

    if not results:
        return SkillResult(text="No printer matching that is configured.")
    return SkillResult(text=" | ".join(results))
