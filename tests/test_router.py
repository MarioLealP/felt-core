from felt_core.router import parse_router_response


def test_well_formed_tool_call():
    raw = '{"skill": "get_weather", "params": {}}'
    assert parse_router_response(raw) == ("get_weather", {})


def test_no_tool_needed():
    assert parse_router_response('{"skill": null}') is None


def test_malformed_json_gets_repaired():
    # Real production bug (2026-09-12): the router occasionally emits a
    # stray quote right before a closing brace — a genuine decision, just
    # invalid JSON, previously silently discarded.
    raw = '{"skill": "get_weather", "params": {}"}'
    assert parse_router_response(raw) == ("get_weather", {})


def test_completely_unparseable_returns_none():
    assert parse_router_response("not json at all") is None


def test_params_preserved_through_repair():
    raw = '{"skill": "poe_price_check", "params": {"item": "Redbeak"}"}'
    assert parse_router_response(raw) == ("poe_price_check", {"item": "Redbeak"})
