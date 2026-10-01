"""WP-29 v2: Exiled Exchange 2 forwards the item text, she prices it."""

import asyncio

from felt_core import events
from felt_core.brain import poe_reactions

UNIQUE = """Item Class: Rings
Rarity: Unique
Death Rush
Amethyst Ring
--------
Requires: Level 60
--------
+7% to Chaos Resistance
"""

CURRENCY = """Item Class: Stackable Currency
Rarity: Currency
Exalted Orb
--------
Stack Size: 3/20
"""

RARE = """Item Class: Body Armours
Rarity: Rare
Doom Shell
Expert Hexer's Robe
--------
"""


def test_parse_unique_currency_rare():
    assert poe_reactions.parse_item_text(UNIQUE) == {
        "name": "Death Rush",
        "base": "Amethyst Ring",
        "rarity": "Unique",
        "kind": "unique",
        "item_class": "Rings",
    }
    cur = poe_reactions.parse_item_text(CURRENCY.replace("\n", "\r\n"))
    assert cur is not None
    assert cur["name"] == "Exalted Orb" and cur["kind"] == "currency" and cur["base"] == ""
    rare = poe_reactions.parse_item_text(RARE)
    assert rare is not None and rare["kind"] == "other"
    assert poe_reactions.parse_item_text("hello there") is None
    assert poe_reactions.parse_item_text("") is None


def test_prompt_uses_the_price_and_never_invents_one():
    priced = events.build_prompt(
        "poe_item",
        {
            "name": "Death Rush",
            "rarity": "Unique",
            "base": "Amethyst Ring",
            "prices": ["Death Rush: 40 divine"],
        },
    )
    assert priced is not None
    assert "40 divine" in priced and "Death Rush" in priced
    unpriced = events.build_prompt(
        "poe_item", {"name": "Doom Shell", "rarity": "Rare", "prices": []}
    )
    assert unpriced is not None
    assert "Do NOT make up a price" in unpriced
    assert events.build_prompt("poe_item", {}) is None


def test_details_are_parsed_and_priced(monkeypatch):
    seen = {}

    async def fake_price(items, config):
        seen["items"] = items
        return ["Exalted Orb: 0.01 divine"]

    monkeypatch.setattr(poe_reactions, "price_items", fake_price)
    out = asyncio.run(events._poe_item_details({"text": CURRENCY}, config=None))  # ty: ignore[invalid-argument-type]
    assert out["name"] == "Exalted Orb" and out["prices"] == ["Exalted Orb: 0.01 divine"]
    assert seen["items"][0]["kind"] == "currency"
    assert asyncio.run(events._poe_item_details({"text": "not an item"}, config=None)) == {}  # ty: ignore[invalid-argument-type]


def test_poe_item_policy_skips_the_chatter_gap():
    p = events.DEFAULT_POLICIES["poe_item"]
    assert p.bypass_min_gap and p.cooldown_s <= 5
