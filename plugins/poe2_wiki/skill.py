"""Path of Exile 2 wiki lookup (WP-29) - the poe2-rag service's hybrid
retrieval (local embeddings + keyword search over the PoE2 wiki), without
its answer step: poe2-rag's /api/ask phrases with cloud Gemini, and Felt is
fully local, so this uses /api/search and hands the passages to her own
model to answer from.
"""

from felt_core.felt_tls import machine_client
from felt_core.plugin import SkillResult

name = "poe2_wiki"
description = (
    "Path of Exile 2 wiki: what a skill, gem, item, passive, mechanic or boss does, how "
    "something works, or if it fits a build. Not prices (poe_price_check), not "
    "web_search."
)
parameters_schema = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "The question or term"}},
    "required": ["query"],
}

_TOP_K = 4
# The passages ride into one felt-llm follow-up (8K context, shared with
# the persona and tools) - keep them to a few thousand characters.
_PASSAGE_CHARS = 700


async def handle(params, ctx):
    query = params.get("query") or ctx.original_message
    # WP-62 M2b B5.3 (D10.1): poe2rag via Caddy's machine host with her Felt
    # cert once FELT_POE2_SEARCH_URL is https://poe2-api.clockmost.com/...
    url = ctx.config.poe2_search_url
    async with machine_client(ctx.config, url, timeout=20) as client:
        response = await client.post(url, json={"query": query, "k": _TOP_K})
        response.raise_for_status()
        hits = response.json().get("hits", [])

    if not hits:
        return SkillResult(text="The PoE2 wiki has nothing on that.")

    blocks = []
    for h in hits:
        where = h["title"] + (f" - {h['section']}" if h.get("section") else "")
        text = " ".join(h["text"].split())
        if len(text) > _PASSAGE_CHARS:
            text = text[:_PASSAGE_CHARS].rsplit(" ", 1)[0] + "..."
        blocks.append(f"[{where}] {text}")
    return SkillResult(
        text="PoE2 wiki passages (answer only from these; PoE1 mechanics don't apply):\n"
        + "\n".join(blocks)
    )
