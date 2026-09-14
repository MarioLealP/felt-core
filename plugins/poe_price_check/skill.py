"""Path of Exile 2 price check — the skill Mario asked for most.

Currency (divine, chaos, etc.) is priced via poe.ninja's public PoE2
economy API — an aggregate is exactly right for currency, since there's
no meaningful notion of "the cheapest listing" for a fungible currency
exchange.

Unique items are different: poe.ninja only gives an aggregate
`listingCount`/`primaryValue`, which Mario found didn't match what the
actual trade site shows (2026-09-14: poe.ninja said 636 listings, the
trade site showed 1577 matched) and can't answer "what's the lowest
listing" or "average of the 3 cheapest" at all — there's no per-listing
data in it. So unique items are priced from the **official trade API**
instead (pathofexile.com/api/trade2), which returns real, individual
listings. That API needs both an item's name AND its base type in the
search query (name-only or type-only searches fail — confirmed live);
the name -> base-type index comes from poe_trade_cache.py's weekly-
refreshed local cache, not a live lookup on every price check.

poe.ninja's aggregate stays as the fallback path (cache not loaded yet,
the item isn't a recognized unique name, or the trade API has zero
listings even including offline sellers) — better a rougher number than
none at all.
"""

from urllib.parse import quote

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult
from felt_core.poe_trade_cache import load_cache

name = "poe_price_check"
description = (
    "Checks the current market price of a Path of Exile 2 item or currency "
    "by name. For unique items, reports the real lowest listing and the "
    "average of the 3 cheapest, from live trade-site listings."
)
parameters_schema = {
    "type": "object",
    "properties": {"item_name": {"type": "string", "description": "The item or currency name to check"}},
    "required": ["item_name"],
}

_NINJA_BASE = "https://poe.ninja/poe2/api/economy"
_TRADE_BASE = "https://www.pathofexile.com/api/trade2"
# Anonymous requests to the trade API get blocked without a real-looking
# User-Agent (confirmed live 2026-09-14).
_TRADE_HEADERS = {
    "User-Agent": "felt-core/0.1 (personal assistant; contact: lealmariop@gmail.com)"
}
_CURRENCY_TYPES = ["Currency", "Fragments", "UncutGems", "Essences", "Runes"]
_UNIQUE_TYPES = [
    "UniqueWeapons",
    "UniqueArmours",
    "UniqueAccessories",
    "UniqueFlasks",
    "UniqueCharms",
    "UniqueJewels",
    "UniqueSanctumRelics",
    "UniqueTablets",
]
_FETCH_COUNT = 10


async def _current_league(client: httpx.AsyncClient, cache: dict | None) -> str:
    if cache and cache.get("leagues"):
        for league in cache["leagues"]:
            if not league.startswith("HC "):
                return league
        return cache["leagues"][0]
    # Fallback: cache missing (first run before the weekly refresh has
    # completed, or the cache file failed to load) — poe.ninja's own
    # league list, the same data poe.ninja price lookups need anyway.
    response = await client.get(f"{_NINJA_BASE}/leagues")
    response.raise_for_status()
    leagues = response.json()
    for league in leagues:
        if not league["id"].startswith("HC "):
            return league["id"]
    return leagues[0]["id"]


def _format_value(core: dict, primary_value: float) -> str:
    """More decimal places for cheap items — 0.00 reads as broken, not
    cheap. Deliberately NOT converting to a secondary currency: the API's
    'rates' field semantics aren't documented/verified, and an unverified
    conversion risks quietly showing a wrong number, which is worse than
    an honest number in an unfamiliar-feeling unit."""
    primary = core.get("primary", "divine")
    decimals = 2 if primary_value >= 1 else 4
    return f"~{primary_value:.{decimals}f} {primary}"


def _format_divine(value: float) -> str:
    decimals = 2 if value >= 1 else 4
    return f"{value:.{decimals}f} divine"


async def _search_currency(client: httpx.AsyncClient, league: str, item_name: str):
    for currency_type in _CURRENCY_TYPES:
        response = await client.get(
            f"{_NINJA_BASE}/exchange/current/overview", params={"league": league, "type": currency_type}
        )
        if response.status_code != 200:
            continue
        data = response.json()
        names = {item["id"]: item["name"] for item in data.get("items", [])}
        for line in data.get("lines", []):
            display_name = names.get(line["id"], line["id"])
            if item_name.lower() in display_name.lower():
                return f"{display_name}: {_format_value(data.get('core', {}), line['primaryValue'])}"
    return None


async def _search_uniques(client: httpx.AsyncClient, league: str, item_name: str):
    for unique_type in _UNIQUE_TYPES:
        response = await client.get(
            f"{_NINJA_BASE}/stash/current/item/overview", params={"league": league, "type": unique_type}
        )
        if response.status_code != 200:
            continue
        data = response.json()
        for line in data.get("lines", []):
            if item_name.lower() in line.get("name", "").lower():
                value = _format_value(data.get("core", {}), line["primaryValue"])
                return f"{line['name']}: {value} ({line.get('listingCount', 0)} listings)"
    return None


def _resolve_item(items_by_name: dict, query: str) -> dict | None:
    """Exact match preferred; otherwise the shortest (most specific)
    entry whose name contains, or is contained in, the query — same
    substring-match convention as _search_uniques above."""
    q = query.strip().lower()
    if q in items_by_name:
        return items_by_name[q]
    candidates = [entry for key, entry in items_by_name.items() if q in key or key in q]
    if not candidates:
        return None
    return min(candidates, key=lambda entry: len(entry["display_name"]))


def _build_search_query(display_name: str, base_type: str, status_option: str) -> dict:
    return {
        "query": {
            "status": {"option": status_option},
            "name": display_name,
            "type": base_type,
            "stats": [{"type": "and", "filters": []}],
        },
        "sort": {"price": "asc"},
    }


async def _post_trade_search(client: httpx.AsyncClient, league: str, query_body: dict) -> dict | None:
    response = await client.post(
        f"{_TRADE_BASE}/search/{quote(league)}", json=query_body, headers=_TRADE_HEADERS
    )
    if response.status_code != 200:
        return None
    return response.json()


async def _fetch_trade_listings(client: httpx.AsyncClient, ids: list[str], search_id: str) -> list[dict]:
    response = await client.get(
        f"{_TRADE_BASE}/fetch/{','.join(ids)}", params={"query": search_id}, headers=_TRADE_HEADERS
    )
    if response.status_code != 200:
        return []
    return response.json().get("result", [])


async def _currency_rates_to_divine(client: httpx.AsyncClient, league: str) -> dict[str, float]:
    """{trade listing currency code: divine-equivalent value} — poe.ninja's
    currency overview uses the exact same short codes ("divine", "chaos",
    "exalted", ...) as the trade API's listing `currency` field, confirmed
    live 2026-09-14, so no separate name-mapping table is needed."""
    response = await client.get(
        f"{_NINJA_BASE}/exchange/current/overview", params={"league": league, "type": "Currency"}
    )
    if response.status_code != 200:
        return {}
    data = response.json()
    return {
        line["id"]: line["primaryValue"]
        for line in data.get("lines", [])
        if "id" in line and "primaryValue" in line
    }


def _convert_to_divine(listings: list[dict], rates: dict[str, float]) -> list[float]:
    """Pure: each listing's price converted to divine, skipping any whose
    currency has no known rate rather than guessing."""
    values = []
    for entry in listings:
        price = entry.get("listing", {}).get("price")
        if not price:
            continue
        rate = rates.get(price.get("currency"))
        if rate is None:
            continue
        values.append(price["amount"] * rate)
    return values


async def _search_real_listings(
    client: httpx.AsyncClient, league: str, display_name: str, base_type: str
) -> dict | None:
    search = await _post_trade_search(
        client, league, _build_search_query(display_name, base_type, "online")
    )
    if search is None:
        return None
    if search.get("total", 0) == 0:
        # No one online selling it right now — widen to offline sellers
        # too, so Mario still gets a real number instead of nothing.
        search = await _post_trade_search(
            client, league, _build_search_query(display_name, base_type, "any")
        )
        if search is None or search.get("total", 0) == 0:
            return None

    ids = search.get("result", [])[:_FETCH_COUNT]
    if not ids:
        return None
    listings = await _fetch_trade_listings(client, ids, search["id"])
    if not listings:
        return None

    rates = await _currency_rates_to_divine(client, league)
    values = sorted(_convert_to_divine(listings, rates))
    if not values:
        return None

    lowest = values[0]
    cheapest = values[:3]
    average = sum(cheapest) / len(cheapest)
    total = search["total"]

    text = (
        f"{display_name}: lowest listing {_format_divine(lowest)}, "
        f"average of the {len(cheapest)} cheapest {_format_divine(average)} "
        f"({total} listings, league: {league})"
    )
    voice_line = (
        f"{display_name}'s lowest listing is {_format_divine(lowest)}, "
        f"with the {len(cheapest)} cheapest averaging {_format_divine(average)}."
    )
    return {"text": text, "voice_line": voice_line}


async def handle(params, ctx):
    config = ctx.config
    item_name = get_single_param(params, "item_name", "item", "name", "query")
    item_name = (item_name or ctx.original_message).strip()

    cache = load_cache(config)

    async with httpx.AsyncClient(timeout=20) as client:
        league = await _current_league(client, cache)

        currency_result = await _search_currency(client, league, item_name)
        if currency_result is not None:
            return SkillResult(text=f"{currency_result} (league: {league})")

        resolved = _resolve_item(cache["items_by_name"], item_name) if cache else None
        if resolved is not None:
            listings = await _search_real_listings(
                client, league, resolved["display_name"], resolved["type"]
            )
            if listings is not None:
                return SkillResult(text=listings["text"], voice_line=listings["voice_line"])

        fallback = await _search_uniques(client, league, item_name)

    if fallback is None:
        return SkillResult(text=f"Couldn't find a current price for '{item_name}' in league {league}.")
    return SkillResult(text=f"{fallback} (league: {league})")
