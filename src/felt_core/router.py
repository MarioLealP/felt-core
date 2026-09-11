"""A small, dedicated model decides tool-vs-chat, separate from the model
that actually talks to the user.

2026-09-11 finding: asking the chat model to both converse *and* decide
whether to emit a tool call was unreliable — llama3.1:8b essentially never
triggered delegate_research for "search the web" style requests, no
matter how the prompt was worded, likely a trained refusal reflex
specific to that framing. Splitting routing into its own narrow
classification call to a small model sidesteps that: it's not being asked
to have a conversation or justify a refusal, just to classify.
"""

import json
import re

import httpx

from felt_core.config import FeltConfig
from felt_core.plugin_loader import LoadedSkill

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _build_router_prompt(registry: dict[str, LoadedSkill]) -> str:
    tool_lines = "\n".join(
        f"- {skill.name}: {skill.description}" for skill in registry.values()
    )
    return (
        "You are a routing classifier, not a conversational assistant. "
        "Given the user's message, decide whether one of these tools "
        "should be called to answer it:\n"
        f"{tool_lines}\n\n"
        "IMPORTANT: if more than one tool could technically apply, always "
        "pick the most SPECIFIC one over a general-purpose one (e.g. "
        "web_search). Worked example:\n"
        'User: "What\'s the weather like right now?"\n'
        'Correct: {"skill": "get_weather", "params": {}}\n'
        'Wrong: {"skill": "web_search", "params": {"query": "weather"}} '
        "— wrong because a dedicated weather tool exists.\n\n"
        'If a tool clearly applies, output exactly: {"skill": "<name>", "params": {...}}\n'
        'If no tool is needed, output exactly: {"skill": null}\n'
        "Output ONLY that JSON object. No explanation, no apology, "
        "no other text."
    )


async def decide_tool(
    message: str, registry: dict[str, LoadedSkill], config: FeltConfig
) -> tuple[str, dict] | None:
    if not registry:
        return None

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{config.ollama_url}/api/chat",
            json={
                "model": config.router_model,
                "messages": [
                    {"role": "system", "content": _build_router_prompt(registry)},
                    {"role": "user", "content": message},
                ],
                "stream": False,
                # Classification, not creative writing — deterministic
                # (temperature 0) greatly reduces run-to-run flakiness on
                # ambiguous cases (2026-09-11 finding: default sampling
                # made the same query route correctly ~50% of the time).
                "options": {"temperature": 0},
            },
        )
        response.raise_for_status()
        raw = response.json()["message"]["content"]

    match = _JSON_RE.search(raw)
    if not match:
        return None
    try:
        call = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    skill_name = call.get("skill")
    if not skill_name:
        return None
    return skill_name, call.get("params", {})
