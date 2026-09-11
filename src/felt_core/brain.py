"""The brain loop: chat, with an optional manual tool-calling round-trip.

Phase 2 (text in -> text out) plus Phase 3/4 (tool-calling + real plugin
loading): if the model's first response is a tool call (per tools.py's
parseable format), the matching skill runs and its result is fed back to
the model for a final natural-language reply. Otherwise the first
response is the answer, one model call, no tool overhead — tool use is
available, never mandatory.
"""

import httpx

from felt_core.config import FeltConfig
from felt_core.plugin_loader import load_plugins
from felt_core.tools import build_system_prompt, parse_tool_call, run_skill


async def _ollama_chat(messages: list[dict], config: FeltConfig) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{config.ollama_url}/api/chat",
            json={"model": config.ollama_model, "messages": messages, "stream": False},
        )
        response.raise_for_status()
        return response.json()["message"]["content"]


async def chat(message: str, config: FeltConfig) -> str:
    registry = load_plugins(config.plugins_dir)
    messages = [
        {"role": "system", "content": build_system_prompt(registry)},
        {"role": "user", "content": message},
    ]
    first_response = await _ollama_chat(messages, config)

    call = parse_tool_call(first_response)
    if call is None:
        return first_response

    skill_name, params = call
    try:
        result = await run_skill(skill_name, params, registry)
        tool_output = result.text or ""
    except ValueError as e:
        tool_output = f"Error: {e}"

    messages += [
        {"role": "assistant", "content": first_response},
        {"role": "user", "content": f"Tool result: {tool_output}\n\nNow answer the original question."},
    ]
    return await _ollama_chat(messages, config)
