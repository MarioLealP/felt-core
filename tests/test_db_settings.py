from felt_core.db.settings import get_setting, set_setting


async def test_get_setting_returns_default_when_unset(tmp_path):
    db_path = str(tmp_path / "felt.db")
    assert await get_setting(db_path, "temperature_unit", "celsius") == "celsius"


async def test_set_then_get_setting_round_trips(tmp_path):
    db_path = str(tmp_path / "felt.db")
    await set_setting(db_path, "temperature_unit", "fahrenheit")
    assert await get_setting(db_path, "temperature_unit", "celsius") == "fahrenheit"


async def test_set_setting_overwrites_existing_value(tmp_path):
    db_path = str(tmp_path / "felt.db")
    await set_setting(db_path, "temperature_unit", "fahrenheit")
    await set_setting(db_path, "temperature_unit", "celsius")
    assert await get_setting(db_path, "temperature_unit") == "celsius"
