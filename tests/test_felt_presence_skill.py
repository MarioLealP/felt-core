import importlib.util
from pathlib import Path

_SKILL_PATH = Path(__file__).resolve().parent.parent / "plugins" / "felt_presence" / "skill.py"


def _load_skill():
    spec = importlib.util.spec_from_file_location("felt_presence_test", _SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


skill = _load_skill()


async def test_shut_up_intent_returns_a_hush_ui_action():
    result = await skill.handle({"intent": "shut_up"}, ctx=None)
    assert result.ui_action.kind == "presence"
    assert result.ui_action.payload == {"action": "hush", "minutes": 10}
    assert result.text  # some short sheepish line, don't pin the exact wording


async def test_unhush_intent_returns_an_unhush_ui_action():
    result = await skill.handle({"intent": "unhush"}, ctx=None)
    assert result.ui_action.kind == "presence"
    assert result.ui_action.payload == {"action": "unhush"}


async def test_missing_intent_defaults_to_hush():
    result = await skill.handle({}, ctx=None)
    assert result.ui_action.payload["action"] == "hush"


async def test_talk_again_phrasing_maps_to_unhush():
    result = await skill.handle({"intent": "you can talk again"}, ctx=None)
    assert result.ui_action.payload == {"action": "unhush"}
