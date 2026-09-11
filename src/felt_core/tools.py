"""Manual tool-calling loop.

Deliberately not dependent on any model's native function-calling support
(settled decision, 2026-09-11 — see assistant-core.md): the model is
prompted to emit tool calls in a fixed, parseable format as part of
otherwise-normal chat output, and this module parses that out. Keeps
model choice free to optimize for chat quality rather than tool-calling
support.
"""

import json
import re

from felt_core.skills_registry import REGISTRY

_TOOL_CALL_RE = re.compile(r"```tool_call\s*\n(.*?)\n```", re.DOTALL)


def build_system_prompt() -> str:
    tool_lines = "\n".join(
        f"- {skill.name}: {skill.description}" for skill in REGISTRY.values()
    )
    return (
        "You can call a tool by responding with ONLY a fenced code block "
        "of this exact form, nothing else:\n"
        '```tool_call\n{"skill": "<name>", "params": {...}}\n```\n'
        f"Available tools:\n{tool_lines}\n"
        "If no tool is needed, just answer normally in plain text."
    )


def parse_tool_call(response: str) -> tuple[str, dict] | None:
    match = _TOOL_CALL_RE.search(response)
    if not match:
        return None
    try:
        call = json.loads(match.group(1))
        return call["skill"], call.get("params", {})
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


async def run_skill(name: str, params: dict):
    skill = REGISTRY.get(name)
    if skill is None:
        raise ValueError(f"Unknown skill: {name}")
    return await skill.handle(params)
