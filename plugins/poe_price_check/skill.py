"""Path of Exile 2 price check — the skill Mario asked for most.

v1: name -> current market price, via poe.ninja's public PoE2 economy
API (no key, no auth). Searches across currency and all unique-item
categories for a name match. The clipboard-item-text / Exiled Exchange 2
parsing approach from the original spike
(scratch/felt-poe-spike/, see assistant-skills.md) is a possible future
upgrade for exact-roll pricing, not needed for a plain "what's X worth."

League is fetched live, never hardcoded — leagues rotate every few
months and a hardcoded one would silently go stale.
"""

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "poe_price_check"
description = (
    "Checks the current market price of a Path of Exile 2 item or currency "
    "by name, via poe.ninja's public economy data."
)
parameters_schema = {
    "type": "object",
    "properties": {"item_name": {"type": "string", "description": "The item or currency name to check"}},
    "required": ["item_name"],
}

_BASE = "https://poe.ninja/poe2/api/economy"
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


async def _current_league(client: httpx.AsyncClient) -> str:
    response = await client.get(f"{_BASE}/leagues")
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


async def _search_currency(client: httpx.AsyncClient, league: str, item_name: str):
    for currency_type in _CURRENCY_TYPES:
        response = await client.get(
            f"{_BASE}/exchange/current/overview", params={"league": league, "type": currency_type}
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
            f"{_BASE}/stash/current/item/overview", params={"league": league, "type": unique_type}
        )
        if response.status_code != 200:
            continue
        data = response.json()
        for line in data.get("lines", []):
            if item_name.lower() in line.get("name", "").lower():
                value = _format_value(data.get("core", {}), line["primaryValue"])
                return f"{line['name']}: {value} ({line.get('listingCount', 0)} listings)"
    return None


async def handle(params, ctx):
    item_name = get_single_param(params, "item_name", "item", "name", "query")
    item_name = (item_name or ctx.original_message).strip()
    async with httpx.AsyncClient(timeout=20) as client:
        league = await _current_league(client)
        result = await _search_currency(client, league, item_name)
        if result is None:
            result = await _search_uniques(client, league, item_name)

    if result is None:
        return SkillResult(text=f"Couldn't find a current price for '{item_name}' in league {league}.")
    return SkillResult(text=f"{result} (league: {league})")
