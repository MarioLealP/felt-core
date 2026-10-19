"""The Ender 3D printer through Home Assistant (2026-10-19: Mario, "can we get
moonraker/mainsail/klipper through printer so felt can tell me when prints
are finished etc?").

Klipper's moonraker publishes the printer over MQTT into HA (device "Ender",
`sensor.ender_*`; workspace runbooks/ender3-max-neo-klipper.md), so she reads
it with her own non-admin HA token like the phone and leave-by. `watch`
polls the state and says it - desk + phone - when a print finishes, fails,
is cancelled or pauses, in her own words (her model; Mario: "everything that
comes through felt should be routed through her llm so it gets her
personality"), a plain line only when felt-llm is down. The last state is
kept in the db, so a felt-core restart neither misses nor repeats one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from felt_core import db, phone
from felt_core.config import FeltConfig
from felt_core.her_line import her_line
from felt_core.proactive import enqueue_proactive

logger = logging.getLogger(__name__)

POLL_S = 30
_TTL_S = 30 * 60  # a "print done" line is still worth saying for a while
_LAST_KEY = "ender_last_state"
_ACTIVE = ("printing", "paused")
_ENDED = ("complete", "cancelled", "error")
_GONE = ("unavailable", "unknown")
LISBON = ZoneInfo("Europe/Lisbon")
_FIELDS = (
    "state",
    "file",
    "progress",
    "time_left",
    "done_at",
    "elapsed",
    "layer",
    "nozzle",
    "nozzle_target",
    "bed",
    "bed_target",
    "message",
)


class EnderUnavailable(Exception):
    """HA not configured or unreachable."""


@dataclass
class Snapshot:
    state: str
    file: str = ""
    progress: float | None = None
    time_left_s: float | None = None
    done_at: datetime | None = None
    elapsed_s: float | None = None
    layer: str = ""
    nozzle: str = ""
    nozzle_target: str = ""
    bed: str = ""
    bed_target: str = ""
    message: str = ""


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _known(v: str | None) -> str:
    return "" if v is None or v in _GONE else v


def snapshot_from(states: dict[str, str]) -> Snapshot:
    """Pure: `field -> HA state string` (missing = not there) to a Snapshot."""
    done = _known(states.get("done_at"))
    try:
        done_at = datetime.fromisoformat(done) if done else None
    except ValueError:
        done_at = None
    return Snapshot(
        state=states.get("state") or "unavailable",
        file=_known(states.get("file")),
        progress=_num(states.get("progress")),
        time_left_s=_num(states.get("time_left")),
        done_at=done_at,
        elapsed_s=_num(states.get("elapsed")),
        layer=_known(states.get("layer")),
        nozzle=_known(states.get("nozzle")),
        nozzle_target=_known(states.get("nozzle_target")),
        bed=_known(states.get("bed")),
        bed_target=_known(states.get("bed_target")),
        message=_known(states.get("message")),
    )


async def _states(config: FeltConfig, fields: tuple[str, ...]) -> dict[str, str]:
    if not (config.ha_url and config.ha_token):
        raise EnderUnavailable("HA not configured")
    headers = {"Authorization": f"Bearer {config.ha_token}"}
    out: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            for f in fields:
                r = await client.get(f"{config.ha_url}/api/states/sensor.ender_{f}")
                if r.status_code == 200:
                    out[f] = r.json()["state"]
                elif r.status_code != 404:
                    raise EnderUnavailable(f"HA {r.status_code}")
    except httpx.HTTPError as e:
        raise EnderUnavailable(repr(e)) from e
    return out


async def snapshot(config: FeltConfig) -> Snapshot:
    return snapshot_from(await _states(config, _FIELDS))


def job_name(file: str) -> str:
    """Pure: "Cube_PLA_11m3s.gcode" -> "Cube_PLA_11m3s"."""
    return file.rsplit("/", 1)[-1].removesuffix(".gcode")


def duration(seconds: float) -> str:
    """Pure: 4380 -> "1h 13m", 300 -> "5m"."""
    m = round(seconds / 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def _temp(actual: str, target: str) -> str:
    a = f"{round(float(actual))}°C" if _num(actual) is not None else "?"
    t = _num(target)
    return f"{a}/{round(t)}°C" if t else a


def describe(s: Snapshot) -> str:
    """Pure: the printer_status answer."""
    if s.state in _GONE:
        return "The Ender is offline (Klipper/Moonraker isn't reporting to Home Assistant)."
    parts = [f"Ender — {s.state}"]
    if s.file and (s.state in _ACTIVE or s.state in _ENDED):
        parts[0] += f" ({job_name(s.file)})"
    if s.state in _ACTIVE:
        if s.progress is not None:
            parts.append(f"{s.progress:.0f}% done")
        if s.layer:
            parts.append(f"layer {s.layer}")
        if s.time_left_s is not None:
            left = f"~{duration(s.time_left_s)} left"
            if s.done_at:
                left += f" (done ~{s.done_at.astimezone(LISBON):%H:%M})"
            parts.append(left)
    if s.nozzle:
        parts.append(f"nozzle {_temp(s.nozzle, s.nozzle_target)}")
    if s.bed:
        parts.append(f"bed {_temp(s.bed, s.bed_target)}")
    if s.message and s.state in ("paused", "error"):
        parts.append(s.message)
    return ", ".join(parts)


def event_for(prev: str | None, cur: str) -> str | None:
    """Pure: the transition worth telling Mario about, if any. No previous
    state (first run) is never an event - she'd announce an old print."""
    if prev is None or prev == cur:
        return None
    if prev in _ACTIVE and cur in _ENDED:
        return cur
    if prev == "printing" and cur == "paused":
        return "paused"
    return None


def build_prompt(event: str, s: Snapshot) -> str:
    """Pure: the facts for her model (her_line) - she words it."""
    name = f' "{job_name(s.file)}"' if s.file else ""
    if event == "complete":
        took = f" It took {duration(s.elapsed_s)}." if s.elapsed_s else ""
        what = f"Mario's 3D printer (the Ender) just finished printing{name}.{took}"
    elif event == "cancelled":
        what = f"Mario's 3D printer (the Ender) print{name} was just cancelled."
    elif event == "error":
        why = f' Klipper says: "{s.message}".' if s.message else ""
        what = f"Mario's 3D printer (the Ender) stopped with an error while printing{name}.{why}"
    else:
        why = f' Klipper says: "{s.message}".' if s.message else ""
        what = f"Mario's 3D printer (the Ender) paused while printing{name}.{why}"
    return what


async def phrase(config: FeltConfig, event: str, s: Snapshot) -> str:
    """Her line for an event; the plain one if felt-llm can't answer."""
    return await her_line(config, build_prompt(event, s), line_for(event, s))


def line_for(event: str, s: Snapshot) -> str:
    """Pure: the plain line, when felt-llm is down."""
    name = f" ({job_name(s.file)})" if s.file else ""
    if event == "complete":
        took = f" after {duration(s.elapsed_s)}" if s.elapsed_s else ""
        return f"The Ender's print is done{name}{took}."
    if event == "cancelled":
        return f"The Ender's print was cancelled{name}."
    if event == "error":
        why = f": {s.message}" if s.message else ""
        return f"The Ender stopped with an error{name}{why}."
    why = f" - {s.message}" if s.message else ""
    return f"The Ender paused{name}{why}."


async def check(config: FeltConfig) -> str | None:
    """One poll: the line she said, if anything happened."""
    cur = (await _states(config, ("state",))).get("state")
    if cur is None or cur in _GONE:
        return None  # offline is not a transition; resume from the last real state
    prev = db.get_setting(config.db_path, _LAST_KEY)
    if prev == cur:
        return None
    db.set_setting(config.db_path, _LAST_KEY, cur)
    event = event_for(prev, cur)
    if event is None:
        return None
    line = await phrase(config, event, await snapshot(config))
    enqueue_proactive(config, "printer", line, ttl_s=_TTL_S)
    try:
        await phone.notify(config, "Ender", line, tag="felt-ender")
    except phone.PhoneUnavailable:
        logger.info("ender: phone unreachable, desk only")
    logger.info("ender: %s -> %s, said %r", prev, cur, line)
    return line


async def watch(config: FeltConfig) -> None:
    if not (config.ha_url and config.ha_token):
        logger.info("ender: HA not configured, not watching the printer")
        return
    while True:
        try:
            await check(config)
        except EnderUnavailable as e:
            logger.debug("ender: HA unreachable (%s)", e)
        except Exception:
            logger.exception("ender: watch tick failed")
        await asyncio.sleep(POLL_S)
