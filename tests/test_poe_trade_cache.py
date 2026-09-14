from datetime import datetime
from zoneinfo import ZoneInfo

from felt_core.poe_trade_cache import _build_name_index, seconds_until_next_refresh

_TZ = ZoneInfo("Europe/Lisbon")


def test_build_name_index_only_keeps_named_unique_entries():
    items_data = {
        "result": [
            {
                "id": "accessory",
                "entries": [
                    {"type": "Prismatic Ring"},  # base type, no name — not a unique
                    {"type": "Prismatic Ring", "name": "The Taming"},
                ],
            }
        ]
    }
    index = _build_name_index(items_data)
    assert index == {"the taming": {"type": "Prismatic Ring", "display_name": "The Taming"}}


def test_build_name_index_lowercases_the_lookup_key_only():
    items_data = {"result": [{"entries": [{"type": "Amulet", "name": "Karui Ward"}]}]}
    index = _build_name_index(items_data)
    assert "karui ward" in index
    assert index["karui ward"]["display_name"] == "Karui Ward"


def test_seconds_until_next_refresh_same_day_before_2300():
    # Friday at 10:00 -> refresh is later that same Friday at 23:00.
    now = datetime(2026, 9, 18, 10, 0, tzinfo=_TZ)  # a Friday
    assert seconds_until_next_refresh(now) == 13 * 3600


def test_seconds_until_next_refresh_same_day_after_2300_rolls_to_next_week():
    # Friday at 23:30 -> refresh already happened, next one is 7 days out.
    now = datetime(2026, 9, 18, 23, 30, tzinfo=_TZ)
    assert seconds_until_next_refresh(now) == 7 * 24 * 3600 - 1800


def test_seconds_until_next_refresh_from_a_different_weekday():
    # Monday -> 4 days until Friday 23:00.
    now = datetime(2026, 9, 14, 0, 0, tzinfo=_TZ)
    expected = 4 * 24 * 3600 + 23 * 3600
    assert seconds_until_next_refresh(now) == expected
