from felt_core.db import get_setting, set_setting


def test_get_setting_returns_default_when_unset(tmp_path):
    db_path = str(tmp_path / "felt.db")
    assert get_setting(db_path, "temperature_unit", "celsius") == "celsius"


def test_set_then_get_setting_round_trips(tmp_path):
    db_path = str(tmp_path / "felt.db")
    set_setting(db_path, "temperature_unit", "fahrenheit")
    assert get_setting(db_path, "temperature_unit", "celsius") == "fahrenheit"


def test_set_setting_overwrites_existing_value(tmp_path):
    db_path = str(tmp_path / "felt.db")
    set_setting(db_path, "temperature_unit", "fahrenheit")
    set_setting(db_path, "temperature_unit", "celsius")
    assert get_setting(db_path, "temperature_unit") == "celsius"
