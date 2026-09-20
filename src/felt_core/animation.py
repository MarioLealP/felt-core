"""Output-to-animation middleware — the animation-side counterpart to how
voice output already gets its own dedicated stage (the /v1/voice-chat
handler's call out to felt-voice's TTS, not something each skill computes
inline). This is the same shape: one small, centralized, independently
testable function that decides what Felt's overlay should react with,
given what a skill actually returned - rather than that decision being
scattered ad-hoc across brain.py or duplicated per skill.

Mario, 2026-09-20: "we already have a middleware for output to voice, do
similar for output to animation, this way we can also use it for
testing."
"""

from felt_core.plugin import SkillResult
from felt_core.plugin_loader import LoadedSkill


def resolve_animation_tag(skill: LoadedSkill | None, result: SkillResult) -> str | None:
    """Decide the final animation tag for a skill's result.

    Priority: the skill's own output-driven override
    (SkillResult.animation_tag - e.g. get_weather escalating to
    "reaction/weather/storm" based on the actual forecast condition) beats
    the skill's static default from plugins/tags.json (LoadedSkill.
    default_tag), which beats no tag at all (None - most skills have
    neither yet, "we map every single needed one after the fact").

    This only resolves WHICH TAG applies - it deliberately stops there.
    Turning a tag into one concrete clip id (fallback-broadening to a
    less-specific tag when nothing's tagged that specifically, weighted
    specific-vs-generic variety, repeat-avoidance) is a separate,
    not-yet-built stage that also belongs in felt-core, per the "godot is
    presentation, felt-core owns every decision" rule (Mario, 2026-09-20)
    - see runbooks/assistant-overlay-behavior.md's "Skill-output-driven
    reactions, and the architecture split" section for that design.
    felt-overlay (the Godot project) never sees a tag at all in the real
    system, only a concrete "play this clip id" instruction; this function
    is just the first of several steps that happen before that instruction
    gets sent, not the whole pipeline.
    """
    if result.animation_tag:
        return result.animation_tag
    if skill is not None and skill.default_tag:
        return skill.default_tag
    return None
