"""Real network calls, recorded once via VCR (see tests/conftest.py) —
the full handle() flow against real poe.ninja / trade-API response
shapes, not just the pure logic already covered in
test_poe_price_check_skill.py."""

import pytest
from conftest import load_plugin_module

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext
from felt_core.poe_trade_cache import refresh

skill = load_plugin_module("poe_price_check/skill.py")


@pytest.mark.vcr
async def test_handle_prices_a_currency():
    config = FeltConfig(poe_trade_cache_path="/nonexistent/poe_trade_cache.json")
    ctx = ExecutionContext(config=config, original_message="price check chaos orb")

    result = await skill.handle({"item_name": "chaos orb"}, ctx)

    assert "Chaos Orb" in result.text
    assert "divine" in result.text


@pytest.mark.vcr
async def test_handle_prices_a_unique_with_real_listings(tmp_path):
    config = FeltConfig(poe_trade_cache_path=str(tmp_path / "poe_trade_cache.json"))
    await refresh(config)
    ctx = ExecutionContext(config=config, original_message="price check the taming")

    result = await skill.handle({"item_name": "the taming"}, ctx)

    assert "The Taming" in result.text
    assert "lowest listing" in result.text
    assert "average of the" in result.text
    assert result.voice_line is not None
