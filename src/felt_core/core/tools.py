"""Skill execution — the actual "run this tool" step.

Routing (deciding whether/which tool to call) lives in router.py now, not
here — see its docstring for why that split exists.
"""

from felt_core.core.plugin_loader import LoadedSkill
from felt_core.models.plugin import ExecutionContext


async def run_skill(name: str, params: dict, registry: dict[str, LoadedSkill], ctx: ExecutionContext):
    """Run the skill registered under `name` with `params`, raising if the name is unknown."""
    skill = registry.get(name)
    if skill is None:
        raise ValueError(f"Unknown skill: {name}")
    return await skill.handle(params, ctx)
