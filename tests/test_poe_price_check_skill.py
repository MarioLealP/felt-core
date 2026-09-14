import importlib.util
from pathlib import Path

_SKILL_PATH = Path(__file__).resolve().parent.parent / "plugins" / "poe_price_check" / "skill.py"


def _load_skill():
    spec = importlib.util.spec_from_file_location("poe_price_check_test", _SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


skill = _load_skill()


def test_resolve_item_exact_match():
    items_by_name = {
        "the taming": {"type": "Prismatic Ring", "display_name": "The Taming"},
        "headhunter": {"type": "Leather Belt", "display_name": "Headhunter"},
    }
    assert skill._resolve_item(items_by_name, "The Taming") == items_by_name["the taming"]


def test_resolve_item_substring_match_prefers_the_more_specific_name():
    items_by_name = {
        "the taming": {"type": "Prismatic Ring", "display_name": "The Taming"},
        "the tempest's binding": {"type": "Ring", "display_name": "The Tempest's Binding"},
    }
    # "taming" only substring-matches one of the two.
    assert skill._resolve_item(items_by_name, "taming") == items_by_name["the taming"]


def test_resolve_item_no_match_returns_none():
    items_by_name = {"the taming": {"type": "Prismatic Ring", "display_name": "The Taming"}}
    assert skill._resolve_item(items_by_name, "some unrelated item") is None


def test_build_search_query_shape():
    query = skill._build_search_query("The Taming", "Prismatic Ring", "online")
    assert query["query"]["name"] == "The Taming"
    assert query["query"]["type"] == "Prismatic Ring"
    assert query["query"]["status"]["option"] == "online"
    assert query["sort"] == {"price": "asc"}


def test_convert_to_divine_uses_known_rates():
    listings = [
        {"listing": {"price": {"amount": 4, "currency": "divine"}}},
        {"listing": {"price": {"amount": 50, "currency": "chaos"}}},
    ]
    rates = {"divine": 1, "chaos": 0.1069}
    values = skill._convert_to_divine(listings, rates)
    assert values == [4.0, 5.345]


def test_convert_to_divine_skips_unknown_currency_and_missing_price():
    listings = [
        {"listing": {"price": {"amount": 1, "currency": "mystery-currency"}}},
        {"listing": {}},
        {"listing": {"price": {"amount": 2, "currency": "divine"}}},
    ]
    rates = {"divine": 1}
    assert skill._convert_to_divine(listings, rates) == [2.0]


def test_format_divine_more_decimals_for_cheap_items():
    assert skill._format_divine(6.0) == "6.00 divine"
    assert skill._format_divine(0.1069) == "0.1069 divine"
