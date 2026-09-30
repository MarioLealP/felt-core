"""WP-29: PoE2 screen reactions price the loot in view."""

import json

from felt_core.brain import poe_reactions, screen_reactions
from felt_core.config import FeltConfig
from felt_core.plugin import SkillResult
from felt_core.plugin_loader import LoadedSkill


def _config(tmp_path, **kwargs):
    kwargs.setdefault("screen_reaction_cooldown_s", 0)
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


def _fake_price_skill(monkeypatch, texts):
    calls = []

    async def handle(params, ctx):
        calls.append(params["item_name"])
        return SkillResult(text=texts.get(params["item_name"], "Couldn't find a current price."))

    skill = LoadedSkill(name="poe_price_check", description="", parameters_schema={}, handle=handle)
    monkeypatch.setattr(poe_reactions, "load_plugins", lambda _dir: {"poe_price_check": skill})
    return calls


def test_is_poe():
    assert poe_reactions.is_poe("pathofexilesteam.exe")
    assert poe_reactions.is_poe("PathOfExile_x64Steam.exe")
    assert not poe_reactions.is_poe("firefox.exe")


def test_snap_unique_name_fixes_misreads(monkeypatch, tmp_path):
    cache = {
        "items_by_name": {"headhunter": {"display_name": "Headhunter", "type": "Leather Belt"}}
    }
    monkeypatch.setattr(poe_reactions, "load_cache", lambda _c: cache)
    config = _config(tmp_path)
    assert poe_reactions.snap_unique_name("Headhuntr", config) == "Headhunter"
    assert poe_reactions.snap_unique_name("Something Else", config) == "Something Else"


async def test_price_items_prices_uniques_and_currency_only(monkeypatch, tmp_path):
    monkeypatch.setattr(poe_reactions, "load_cache", lambda _c: None)
    calls = _fake_price_skill(monkeypatch, {"Divine Orb": "Divine Orb: 1 = 380 exalted"})
    prices = await poe_reactions.price_items(
        [
            {"name": "Divine Orb", "kind": "currency"},
            {"name": "Iron Sword", "kind": "other"},
            {"name": "Nobody Knows This", "kind": "unique"},
        ],
        _config(tmp_path),
    )
    assert prices == ["Divine Orb: 1 = 380 exalted"]
    assert calls == ["Divine Orb", "Nobody Knows This"]


async def test_poe_screen_reaction_speaks_the_price(monkeypatch, tmp_path, fake_llm):
    monkeypatch.setattr(poe_reactions, "load_cache", lambda _c: None)
    _fake_price_skill(monkeypatch, {"Divine Orb": "Divine Orb: 1 = 380 exalted"})
    fake_llm.script = [
        json.dumps(
            {
                "seen": "a Divine Orb tooltip",
                "line": "ooh shiny",
                "emotion": "neutral",
                "items": [{"name": "Divine Orb", "kind": "currency"}],
            }
        ),
        "A Divine! That's like three hundred eighty exalts, pocket it.",
    ]
    result = await screen_reactions.react_to_screen(
        b"jpeg", "pathofexilesteam.exe", "Path of Exile 2", _config(tmp_path)
    )
    assert result.reply == "A Divine! That's like three hundred eighty exalts, pocket it."
    assert result.emotion == "excited"
    assert fake_llm.calls[0]["json_schema"] is poe_reactions.POE_SCHEMA
    assert "380 exalted" in fake_llm.calls[1]["messages"][-1]["content"]


async def test_poe_reaction_without_items_is_one_call(monkeypatch, tmp_path, fake_llm):
    _fake_price_skill(monkeypatch, {})
    fake_llm.script = [
        json.dumps(
            {"seen": "a boss fight", "line": "don't die!", "emotion": "excited", "items": []}
        )
    ]
    result = await screen_reactions.react_to_screen(
        b"jpeg", "pathofexilesteam.exe", "", _config(tmp_path)
    )
    assert result.reply == "don't die!"
    assert len(fake_llm.calls) == 1


async def test_other_apps_keep_the_plain_schema(tmp_path, fake_llm):
    fake_llm.script = [json.dumps({"seen": "x", "line": "nice", "emotion": "happy"})]
    await screen_reactions.react_to_screen(b"jpeg", "firefox.exe", "", _config(tmp_path))
    assert fake_llm.calls[0]["json_schema"] is screen_reactions._SCREEN_REACT_SCHEMA
