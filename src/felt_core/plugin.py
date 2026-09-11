"""The plugin interface every Felt skill implements.

Skills live under the configured plugins_dir, one subdirectory each,
discovered and loaded at startup. Dropping a new folder in adds a skill;
deleting one removes it — no core code changes needed.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class UIAction:
    kind: str  # e.g. "toast", "show_image"
    payload: dict[str, Any]


@dataclass
class SkillResult:
    text: str | None = None
    voice_line: str | None = None
    ui_action: UIAction | None = None


class ExecutionContext(Protocol):
    """Whatever a skill needs from the brain at call time (config, http client, etc.)."""


class Skill(Protocol):
    name: str
    description: str
    parameters_schema: dict[str, Any]

    async def handle(self, params: dict[str, Any], ctx: ExecutionContext) -> SkillResult: ...
