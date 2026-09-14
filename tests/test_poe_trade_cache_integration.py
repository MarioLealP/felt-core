"""Real network calls, recorded once via VCR (see tests/conftest.py) —
these exercise refresh()'s actual HTTP behavior against the real trade
API response shape, not just the pure parsing logic already covered in
test_poe_trade_cache.py."""

import pytest

from felt_core.config import FeltConfig
from felt_core.poe_trade_cache import load_cache, refresh


@pytest.mark.vcr
async def test_refresh_populates_a_loadable_cache(tmp_path):
    config = FeltConfig(poe_trade_cache_path=str(tmp_path / "poe_trade_cache.json"))

    await refresh(config)

    cache = load_cache(config)
    assert cache is not None
    assert "Forbidden Rites" in cache["leagues"]
    assert cache["items_by_name"]["the taming"] == {
        "type": "Prismatic Ring",
        "display_name": "The Taming",
    }
