from felt_core.animation import resolve_animation_tag
from felt_core.plugin import SkillResult
from felt_core.plugin_loader import LoadedSkill


async def _unused_handle(params, ctx):
    raise AssertionError("not expected to be called in these tests")


def _skill(default_tag: str | None) -> LoadedSkill:
    return LoadedSkill(
        name="example_skill",
        description="",
        parameters_schema={},
        handle=_unused_handle,  # not called in these tests
        default_tag=default_tag,
    )


def test_skill_output_override_wins_over_manifest_default():
    skill = _skill(default_tag="reaction/weather")
    result = SkillResult(text="storm's coming", animation_tag="reaction/weather/storm")

    assert resolve_animation_tag(skill, result) == "reaction/weather/storm"


def test_falls_back_to_manifest_default_when_skill_sets_no_override():
    skill = _skill(default_tag="reaction/weather")
    result = SkillResult(text="15 degrees")

    assert resolve_animation_tag(skill, result) == "reaction/weather"


def test_no_tag_at_all_when_neither_is_set():
    skill = _skill(default_tag=None)
    result = SkillResult(text="the light is on")

    assert resolve_animation_tag(skill, result) is None


def test_no_skill_but_result_has_an_override_still_resolves():
    # Real case: run_skill raising before a LoadedSkill lookup succeeds,
    # or a caller that only has the result. The skill's own override
    # should still win even with no registry entry to fall back to.
    result = SkillResult(text="storm's coming", animation_tag="reaction/weather/storm")

    assert resolve_animation_tag(None, result) == "reaction/weather/storm"


def test_no_skill_and_no_override_is_none():
    result = SkillResult(text="the light is on")

    assert resolve_animation_tag(None, result) is None
