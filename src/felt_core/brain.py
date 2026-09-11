"""The brain loop: a small router decides tool-vs-chat, the main model
only ever converses.

2026-09-11 redesign: previously the chat model itself was prompted to
emit a fenced tool_call block when it decided a tool applied — unreliable
in practice (see router.py's docstring). Now routing is a separate,
narrow classification call to a small dedicated model
(config.router_model); the main model (config.ollama_model, chosen for
chat quality) never sees tool-call instructions at all, it just answers —
either the original question, or the original question plus a tool's
result to weave into a natural reply.
"""

import httpx

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext
from felt_core.plugin_loader import load_plugins
from felt_core.router import decide_tool
from felt_core.tools import run_skill


async def _ollama_chat(messages: list[dict], model: str, config: FeltConfig) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{config.ollama_url}/api/chat",
            json={"model": model, "messages": messages, "stream": False},
        )
        response.raise_for_status()
        return response.json()["message"]["content"]


async def chat(message: str, config: FeltConfig) -> str:
    registry = load_plugins(config.plugins_dir)

    call = await decide_tool(message, registry, config)
    if call is None:
        return await _ollama_chat(
            [{"role": "user", "content": message}], config.ollama_model, config
        )

    skill_name, params = call
    try:
        ctx = ExecutionContext(config=config, original_message=message)
        result = await run_skill(skill_name, params, registry, ctx)
        tool_output = result.text or ""
    except ValueError as e:
        tool_output = f"Error: {e}"

    return await _ollama_chat(
        [
            {"role": "user", "content": message},
            {
                "role": "user",
                "content": (
                    f"(Tool result: {tool_output})\n\n"
                    "State that fact directly and naturally, as if you already knew it. "
                    "Do not mention tools, and do not add any disclaimer about lacking "
                    "real-time access — the tool result above IS current and accurate."
                ),
            },
        ],
        config.ollama_model,
        config,
    )
