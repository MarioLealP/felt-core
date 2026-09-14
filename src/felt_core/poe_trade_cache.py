"""Local cache of Path of Exile 2 trade-API reference data — the item
name -> base-type index needed to search real listings (the official
trade API requires both, e.g. name="The Taming", type="Prismatic Ring";
name-only or type-only searches fail, confirmed live 2026-09-14) and the
current league list.

Refreshed weekly rather than on every price check — this reference data
changes rarely (leagues rotate on a multi-week cycle, items only when a
patch adds/removes uniques), and it's polite to the (unauthenticated,
rate-limited) trade API to not re-fetch a ~180KB item list on every
single price check.

Mario, 2026-09-14: "cache all items locally, update it once a week or
something, same for leagues (They come out on fridays so just run it on
friday at 23:00)." So the weekly refresh is anchored to Friday 23:00
Europe/Lisbon (his timezone — felt-core's container runs UTC, confirmed
2026-09-14) rather than an arbitrary day: a new league is live by then,
so the cache picks it up same-day instead of up to a week late.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from felt_core.config import FeltConfig

logger = logging.getLogger(__name__)

_TRADE_BASE = "https://www.pathofexile.com/api/trade2"
# Anonymous, unauthenticated requests to the trade API get blocked
# without a real-looking User-Agent (confirmed live 2026-09-14).
_TRADE_HEADERS = {
    "User-Agent": "felt-core/0.1 (personal assistant; contact: lealmariop@gmail.com)"
}
_REFRESH_TZ = ZoneInfo("Europe/Lisbon")
_REFRESH_WEEKDAY = 4  # Monday=0 .. Friday=4
_REFRESH_HOUR = 23


def _build_name_index(items_data: dict) -> dict[str, dict]:
    """Pure: {lowercased unique item name: {"type", "display_name"}} from
    the raw /data/items response. Only entries with a "name" field are
    actual unique items (e.g. {"type": "Prismatic Ring", "name": "The
    Taming"}) — base types, gems, and currency have no name to look up
    by, and are deliberately excluded rather than kept as noise. The
    original-case name is kept alongside the lookup key so callers can
    display "The Taming" rather than whatever case the user typed."""
    index: dict[str, dict] = {}
    for group in items_data.get("result", []):
        for entry in group.get("entries", []):
            item_name = entry.get("name")
            if item_name:
                index[item_name.lower()] = {"type": entry["type"], "display_name": item_name}
    return index


async def _fetch_json(client: httpx.AsyncClient, path: str) -> dict:
    response = await client.get(f"{_TRADE_BASE}{path}", headers=_TRADE_HEADERS)
    response.raise_for_status()
    return response.json()


async def refresh(config: FeltConfig) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        items_data = await _fetch_json(client, "/data/items")
        leagues_data = await _fetch_json(client, "/data/leagues")

    cache = {
        "updated_at": datetime.now(tz=_REFRESH_TZ).isoformat(),
        "leagues": [league["id"] for league in leagues_data.get("result", [])],
        "items_by_name": _build_name_index(items_data),
    }

    path = Path(config.poe_trade_cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a crash mid-write can never leave a truncated,
    # unparseable cache file behind for load_cache to trip over.
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(cache))
    tmp_path.replace(path)
    logger.info(
        "poe trade cache refreshed: %d unique names, %d leagues",
        len(cache["items_by_name"]),
        len(cache["leagues"]),
    )


def load_cache(config: FeltConfig) -> dict | None:
    """Returns None if there's no cache yet (first run before the
    initial refresh has completed) or it's unreadable — callers fall
    back to poe.ninja-only behavior in that case, same "degrade, don't
    crash" convention as everything else in this project."""
    path = Path(config.poe_trade_cache_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def seconds_until_next_refresh(now: datetime) -> float:
    """Pure, unit-testable: seconds from `now` (tz-aware, Europe/Lisbon)
    until the next Friday 23:00 in that timezone."""
    days_ahead = (_REFRESH_WEEKDAY - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=_REFRESH_HOUR, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


async def watch(config: FeltConfig) -> None:
    if load_cache(config) is None:
        # No cache at all yet (first run ever) — get one immediately
        # rather than leaving price checks degraded for up to a week.
        try:
            await refresh(config)
        except Exception:
            logger.exception("initial poe trade cache refresh failed")

    while True:
        delay = seconds_until_next_refresh(datetime.now(tz=_REFRESH_TZ))
        await asyncio.sleep(delay)
        try:
            await refresh(config)
        except Exception:
            logger.exception("poe trade cache refresh failed, will retry next week")
