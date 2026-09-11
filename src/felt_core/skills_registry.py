"""Temporary in-code skill registry for Brain v0's tool-calling proof.

Phase 4 replaces this with a real directory-based plugin loader
(`plugins/`, per plugin.py's Skill protocol). This module exists only to
prove the manual tool-calling loop end-to-end with one trivial skill
before building the real loader on top of it.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from felt_core.plugin import SkillResult


@dataclass
class RegisteredSkill:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    handle: Callable[[dict[str, Any]], Awaitable[SkillResult]]


async def _get_time(params: dict[str, Any]) -> SkillResult:
    return SkillResult(text=datetime.now().isoformat(timespec="seconds"))


REGISTRY: dict[str, RegisteredSkill] = {
    "get_time": RegisteredSkill(
        name="get_time",
        description="Returns the current local date and time. No parameters.",
        parameters_schema={"type": "object", "properties": {}},
        handle=_get_time,
    ),
}
