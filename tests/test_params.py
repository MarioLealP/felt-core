from felt_core.params import get_single_param


def test_exact_key_match():
    assert get_single_param({"item_name": "Redbeak"}, "item_name") == "Redbeak"


def test_alternate_key_match():
    # The real, repeated router finding: right value, wrong key.
    assert get_single_param({"item": "Redbeak"}, "item_name", "item") == "Redbeak"


def test_single_param_fallback_absorbs_any_key_name():
    # 2026-09-12: router sent {"location": "your_city_or_location"} for a
    # weather skill expecting "day" — this fallback is what saved that case.
    assert get_single_param({"location": "tomorrow"}, "day", "when") == "tomorrow"


def test_no_match_and_multiple_params_returns_none():
    assert get_single_param({"a": 1, "b": 2}, "day", "when") is None


def test_empty_params_returns_none():
    assert get_single_param({}, "day") is None
