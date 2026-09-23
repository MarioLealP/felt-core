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


# -- WP-10: the new intents -----------------------------------------------


async def test_go_away_for_a_bit_maps_to_hide():
    result = await skill.handle({"intent": "go away for a bit"}, ctx=None)
    assert result.ui_action.payload == {"action": "hide"}


async def test_give_me_five_minutes_maps_to_brb():
    result = await skill.handle({"intent": "give me five minutes"}, ctx=None)
    assert result.ui_action.payload == {"action": "brb"}


async def test_come_back_maps_to_summon_not_come():
    result = await skill.handle({"intent": "come back"}, ctx=None)
    assert result.ui_action.payload == {"action": "summon"}


async def test_come_here_maps_to_come():
    result = await skill.handle({"intent": "come here"}, ctx=None)
    assert result.ui_action.payload == {"action": "come"}


async def test_move_over_maps_to_move():
    result = await skill.handle({"intent": "move over"}, ctx=None)
    assert result.ui_action.payload == {"action": "move"}


async def test_look_at_this_maps_to_look_at_this_action():
    result = await skill.handle({"intent": "look at this"}, ctx=None)
    assert result.ui_action.payload == {"action": "look_at_this"}


async def test_what_did_you_say_maps_to_repeat():
    result = await skill.handle({"intent": "what did you say?"}, ctx=None)
    assert result.ui_action.payload == {"action": "repeat"}


async def test_all_wp10_intents_are_kind_presence():
    for intent in ("hide", "brb", "come_back", "come", "move", "look", "repeat"):
        result = await skill.handle({"intent": intent}, ctx=None)
        assert result.ui_action.kind == "presence"
        assert result.text  # always says something short, never silent
