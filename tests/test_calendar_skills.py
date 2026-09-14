import importlib.util
from pathlib import Path

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _load_skill(plugin_name: str):
    path = _PLUGINS_DIR / plugin_name / "skill.py"
    spec = importlib.util.spec_from_file_location(f"{plugin_name}_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_list_calendar_events_reports_unconfigured():
    skill = _load_skill("list_calendar_events")
    config = FeltConfig(google_calendar_refresh_token=None)
    ctx = ExecutionContext(config=config, original_message="what's on my calendar today?")

    result = await skill.handle({}, ctx)

    assert "isn't connected" in result.text


async def test_create_calendar_event_reports_unconfigured():
    skill = _load_skill("create_calendar_event")
    config = FeltConfig(google_calendar_refresh_token=None)
    ctx = ExecutionContext(config=config, original_message="add a meeting")

    result = await skill.handle({"title": "Meeting", "when": "tomorrow at 3pm"}, ctx)

    assert "isn't connected" in result.text


async def test_create_calendar_event_requires_both_title_and_when():
    skill = _load_skill("create_calendar_event")
    config = FeltConfig(google_calendar_refresh_token="dummy-token")
    ctx = ExecutionContext(config=config, original_message="add an event")

    result = await skill.handle({"title": "Meeting"}, ctx)

    assert "need both" in result.text.lower()


async def test_create_calendar_event_rejects_unparseable_time():
    skill = _load_skill("create_calendar_event")
    config = FeltConfig(google_calendar_refresh_token="dummy-token")
    ctx = ExecutionContext(config=config, original_message="add an event")

    result = await skill.handle({"title": "Meeting", "when": "asdfghjkl"}, ctx)

    assert "couldn't figure out" in result.text.lower()
