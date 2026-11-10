"""The plugin interface every Felt skill implements.

Skills live under the configured plugins_dir, one subdirectory each,
discovered and loaded at startup. Dropping a new folder in adds a skill;
deleting one removes it — no core code changes needed.
"""

from typing import Any, Protocol

from pydantic import BaseModel

from felt_core.config import FeltConfig  # a runtime import: pydantic validates the field against it


class UIAction(BaseModel):
    kind: str  # e.g. "toast", "show_image"
    payload: dict[str, Any]


class SkillResult(BaseModel):
    text: str | None = None
    voice_line: str | None = None
    ui_action: UIAction | None = None
    # Overrides the skill's default tag from plugins/tags.json when the
    # skill's own OUTPUT (not just "this skill ran") warrants something
    # more specific - e.g. get_weather defaults to "reaction/weather" via
    # the manifest, but sets "reaction/weather/storm" here when the actual
    # forecast condition is severe. None means "use the manifest default
    # for this skill, whatever it is" - most skills won't set this.
    animation_tag: str | None = None


class ExecutionContext(BaseModel):
    """What a skill gets at call time.

    `original_message` exists because the router (a small model) doesn't
    always populate structured params correctly (2026-09-11 finding) — a
    skill that needs a query string can fall back to the raw message
    rather than KeyError-ing on a missing param.
    """

    config: FeltConfig
    original_message: str = ""


class Skill(Protocol):
    name: str
    description: str
    parameters_schema: dict[str, Any]

    async def handle(self, params: dict[str, Any], ctx: ExecutionContext) -> SkillResult: ...
