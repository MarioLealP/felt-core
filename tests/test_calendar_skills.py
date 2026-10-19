from conftest import load_plugin_module

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext


def _load_skill(plugin_name: str):
    return load_plugin_module(f"{plugin_name}/skill.py")


async def test_list_calendar_events_reports_unconfigured():
    skill = _load_skill("list_calendar_events")
    config = FeltConfig(ha_url=None)
    ctx = ExecutionContext(config=config, original_message="what's on my calendar today?")

    result = await skill.handle({}, ctx)

    assert "isn't connected" in result.text


async def test_create_calendar_event_reports_unconfigured():
    skill = _load_skill("create_calendar_event")
    config = FeltConfig(ha_url=None)
    ctx = ExecutionContext(config=config, original_message="add a meeting")

    result = await skill.handle({"title": "Meeting", "when": "tomorrow at 3pm"}, ctx)

    assert "isn't connected" in result.text


async def test_create_calendar_event_requires_both_title_and_when():
    skill = _load_skill("create_calendar_event")
    config = FeltConfig(ha_url="http://ha", ha_token="dummy-token")
    ctx = ExecutionContext(config=config, original_message="add an event")

    result = await skill.handle({"title": "Meeting"}, ctx)

    assert "need both" in result.text.lower()


async def test_create_calendar_event_rejects_unparseable_time():
    skill = _load_skill("create_calendar_event")
    config = FeltConfig(ha_url="http://ha", ha_token="dummy-token")
    ctx = ExecutionContext(config=config, original_message="add an event")

    result = await skill.handle({"title": "Meeting", "when": "asdfghjkl"}, ctx)

    assert "couldn't figure out" in result.text.lower()
