import importlib.util
from pathlib import Path

_SKILL_PATH = Path(__file__).resolve().parent.parent / "plugins" / "get_weather" / "skill.py"


def _load_skill():
    spec = importlib.util.spec_from_file_location("get_weather_test", _SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_clear_sky_maps_to_sun():
    skill = _load_skill()
    assert skill._condition_tag(0) == "reaction/weather/sun"
    assert skill._condition_tag(1) == "reaction/weather/sun"


def test_rain_codes_map_to_rain():
    skill = _load_skill()
    for code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
        assert skill._condition_tag(code) == "reaction/weather/rain"


def test_snow_codes_map_to_snow():
    skill = _load_skill()
    for code in (71, 73, 75):
        assert skill._condition_tag(code) == "reaction/weather/snow"


def test_thunderstorm_codes_map_to_storm():
    skill = _load_skill()
    for code in (95, 96, 99):
        assert skill._condition_tag(code) == "reaction/weather/storm"


def test_unmapped_condition_falls_through_to_none():
    # Partly cloudy / overcast / fog aren't distinctive enough to warrant
    # their own animation yet - None means "just use this skill's
    # plugins/tags.json default" (reaction/weather), not an error.
    skill = _load_skill()
    for code in (2, 3, 45, 48):
        assert skill._condition_tag(code) is None


def test_unknown_code_is_also_none_not_an_error():
    skill = _load_skill()
    assert skill._condition_tag(12345) is None
