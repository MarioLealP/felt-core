"""Cloud delegation as a tool, not a router swap.

Proves the "local model is the default brain, cloud is opt-in per call"
principle by construction: this is just another skill the model can
choose to invoke, exactly like get_time. There is no separate code path
that silently sends things to the cloud — if this skill is never called,
nothing ever leaves the LAN.

Targets ninerouter (see ai-stack.md), Mario's existing gateway to Gemini
via his Antigravity OAuth quota. Configured via FELT_NINEROUTER_URL /
FELT_NINEROUTER_API_KEY — unset means cloud delegation is simply
unavailable, not a hardcoded fallback.
"""

import httpx

from felt_core.plugin import SkillResult

name = "delegate_research"
description = (
    "Delegates a research/web-search question to a cloud model, for things "
    "the local model can't reliably know on its own (current events, "
    "obscure facts, anything needing an actual web search). Only use this "
    "when local knowledge genuinely isn't enough — not a default."
)
parameters_schema = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "The question to research"}},
    "required": ["query"],
}


async def handle(params, ctx):
    config = ctx.config
    if not config.ninerouter_url or not config.ninerouter_api_key:
        return SkillResult(text="Cloud delegation isn't configured (no ninerouter URL/key set).")

    # The router doesn't always populate structured params correctly —
    # fall back to the raw user message rather than erroring.
    query = params.get("query") or ctx.original_message

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{config.ninerouter_url}/chat/completions",
            headers={"Authorization": f"Bearer {config.ninerouter_api_key}"},
            json={
                "model": "ag/gemini-3.8-flash",
                "messages": [{"role": "user", "content": query}],
                "stream": False,
            },
        )
        response.raise_for_status()
        return SkillResult(text=response.json()["choices"][0]["message"]["content"])
