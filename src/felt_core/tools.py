"""Manual tool-calling loop.

Deliberately not dependent on any model's native function-calling support
(settled decision, 2026-09-11 — see assistant-core.md): the model is
prompted to emit tool calls in a fixed, parseable format as part of
otherwise-normal chat output, and this module parses that out. Keeps
model choice free to optimize for chat quality rather than tool-calling
support. Tool use is available, never mandatory — a plain question just
gets a plain answer, one model call, no tool round-trip.

Parsing is deliberately forgiving (2026-09-11 finding): a real local
model reliably decides to call a tool once the prompt is directive enough
with a worked example, but does NOT reliably match the exact requested
fence syntax — observed cases missing the `tool_call` language tag and
missing the closing fence entirely. The parser tries progressively looser
matches rather than requiring one exact format.
"""

import json
import re

from felt_core.plugin import ExecutionContext
from felt_core.plugin_loader import LoadedSkill

_FENCED_RE = re.compile(r"```(?:\w+)?\s*\n?(.*?)\n?```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"\{[^{}]*\"skill\"[^{}]*\}", re.DOTALL)


def build_system_prompt(registry: dict[str, LoadedSkill]) -> str:
    if not registry:
        return "Answer normally in plain text."
    tool_lines = "\n".join(
        f"- {skill.name}: {skill.description}" for skill in registry.values()
    )
    example_skill = next(iter(registry.values()))
    return (
        "You have tools available. If one of them is genuinely needed to "
        "answer, you MUST respond with ONLY a fenced block of this exact "
        "form, nothing else, no apology, no explanation:\n"
        '```tool_call\n{"skill": "<name>", "params": {...}}\n```\n'
        f"Available tools:\n{tool_lines}\n\n"
        "Example:\n"
        f'```tool_call\n{{"skill": "{example_skill.name}", "params": {{}}}}\n```\n'
        "If a tool applies, use it — do NOT apologize or explain that you "
        "lack real-time access/internet/current information; that's "
        "exactly what a tool is for. Only answer in plain text if no tool "
        "genuinely applies."
    )


def parse_tool_call(response: str) -> tuple[str, dict] | None:
    candidates = _FENCED_RE.findall(response) + [response]
    for candidate in candidates:
        try:
            call = json.loads(candidate.strip())
            if isinstance(call, dict) and "skill" in call:
                return call["skill"], call.get("params", {})
        except (json.JSONDecodeError, TypeError):
            continue

    match = _BARE_JSON_RE.search(response)
    if match:
        try:
            call = json.loads(match.group(0))
            return call["skill"], call.get("params", {})
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    return None


async def run_skill(name: str, params: dict, registry: dict[str, LoadedSkill], ctx: ExecutionContext):
    skill = registry.get(name)
    if skill is None:
        raise ValueError(f"Unknown skill: {name}")
    return await skill.handle(params, ctx)
