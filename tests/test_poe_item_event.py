"""WP-29 v2: Exiled Exchange 2 forwards the item text, she prices it."""

import asyncio

import felt_core.core.event_policy as event_policy
import felt_core.core.event_prompt as event_prompt
from felt_core.brain import poe_reactions
from felt_core.core import events

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
    assert cur["name"] == "Exalted Orb"
    assert cur["kind"] == "currency"
    assert cur["base"] == ""
    rare = poe_reactions.parse_item_text(RARE)
    assert rare is not None
    assert rare["kind"] == "other"
    assert poe_reactions.parse_item_text("hello there") is None
    assert poe_reactions.parse_item_text("") is None


def test_prompt_needs_a_real_price():
    priced = event_prompt.build_prompt(
        "poe_item",
        {
            "name": "Death Rush",
            "rarity": "Unique",
            "base": "Amethyst Ring",
            "prices": ["Death Rush: 40 divine"],
        },
    )
    assert priced is not None
    assert "40 divine" in priced
    assert "Death Rush" in priced
    assert event_prompt.build_prompt("poe_item", {"name": "Doom Shell", "rarity": "Rare", "prices": []}) is None
    assert event_prompt.build_prompt("poe_item", {}) is None


def test_details_are_parsed_and_priced(monkeypatch):
    seen = {}

    async def fake_price(items, config):
        seen["items"] = items
        return ["Exalted Orb: 0.01 divine"]

    monkeypatch.setattr(poe_reactions, "price_items", fake_price)
    out = asyncio.run(events._poe_item_details({"text": CURRENCY}, config=None))  # ty: ignore[invalid-argument-type]
    assert out["name"] == "Exalted Orb"
    assert out["prices"] == ["Exalted Orb: 0.01 divine"]
    assert seen["items"][0]["kind"] == "currency"
    assert asyncio.run(events._poe_item_details({"text": "not an item"}, config=None)) == {}  # ty: ignore[invalid-argument-type]


def test_poe_item_policy_skips_the_chatter_gap():
    p = event_policy.DEFAULT_POLICIES["poe_item"]
    assert p.bypass_min_gap
    assert p.cooldown_s <= 5
