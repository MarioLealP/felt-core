"""The plugin interface every Felt skill implements.

Skills live under the configured plugins_dir, one subdirectory each,
discovered and loaded at startup. Dropping a new folder in adds a skill;
deleting one removes it — no core code changes needed.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from felt_core.config import FeltConfig


@dataclass
class UIAction:
    kind: str  # e.g. "toast", "show_image"
    payload: dict[str, Any]


@dataclass
class SkillResult:
    text: str | None = None
    voice_line: str | None = None
    ui_action: UIAction | None = None


@dataclass
class ExecutionContext:
    """What a skill gets at call time.

    `original_message` exists because the router (a small model) doesn't
    always populate structured params correctly (2026-09-11 finding) — a
    skill that needs a query string can fall back to the raw message
    rather than KeyError-ing on a missing param."""

    config: "FeltConfig"
    original_message: str = ""


class Skill(Protocol):
    name: str
    description: str
    parameters_schema: dict[str, Any]

    async def handle(self, params: dict[str, Any], ctx: ExecutionContext) -> SkillResult: ...
