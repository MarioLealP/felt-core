"""Leave-by reminders (Mario 2026-10-18: "i have an appointment tuesday ...
it has a location, can we check how long to go there, and have a automated
leave in 15min to get there in time kind of deal?").

For a calendar event with an address (only those - and always by car), the
drive time comes from Home Assistant's Waze action (free, live traffic;
felt-ha-hub: she asks HA, with her non-admin user): from where Mario's
phone is (the HA app's location, if fresh) or else from home. Leave time =
start - drive - BUFFER. calendar_headsup sends "leave in 15" and "time to
go", re-checking the traffic each time.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

import httpx

from felt_core.config import FeltConfig

logger = logging.getLogger(__name__)

# Mario: "i'm not to hot on driving nor like to arrive late, so give me a 15-20min
# buffer, I prefer get there early and have a coffee if needed" - parking included
BUFFER_MIN = 20
SOON_MIN = 15  # the first nudge: "leave in 15"
_FRESH = timedelta(minutes=30)  # a phone location older than this isn't "where he is"


def has_address(event: dict) -> bool:
    """Pure: a location that looks like an address (a number or a comma),
    not "Mom's place" or "online"."""
    loc = (event.get("location") or "").strip()
    return bool(loc) and (any(ch.isdigit() for ch in loc) or "," in loc)


def leave_time(start: datetime, drive_min: float) -> datetime:
    return start - timedelta(minutes=math.ceil(drive_min) + BUFFER_MIN)


async def _origin(client: httpx.AsyncClient, config: FeltConfig, now: datetime) -> str:
    """Mario's phone position if HA has a fresh one, else home."""
    try:
        r = await client.get(f"{config.ha_url}/api/states/person.mario")
        if r.status_code == 200:
            s = r.json()
            a = s.get("attributes", {})
            updated = datetime.fromisoformat(s["last_updated"])
            if "latitude" in a and now - updated < _FRESH and s.get("state") != "home":
                return f"{a['latitude']},{a['longitude']}"
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    return "zone.home"


async def drive_minutes(
    config: FeltConfig, destination: str, now: datetime | None = None
) -> float | None:
    """Fastest car route right now, in minutes; None if HA/Waze can't say."""
    if not (config.ha_url and config.ha_token):
        return None
    now = now or datetime.now(UTC)
    headers = {"Authorization": f"Bearer {config.ha_token}"}
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            origin = await _origin(client, config, now)
            r = await client.post(
                f"{config.ha_url}/api/services/waze_travel_time/get_travel_times?return_response",
                json={
                    "origin": origin,
                    "destination": destination,
                    "region": "eu",
                    "vehicle_type": "car",
                },
            )
        r.raise_for_status()
        routes = (r.json().get("service_response") or {}).get("routes") or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("leave-by: travel time for %r failed (%r)", destination[:60], e)
        return None
    times = [float(x["duration"]) for x in routes if x.get("duration")]
    return min(times) if times else None
