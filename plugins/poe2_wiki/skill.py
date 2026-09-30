"""Path of Exile 2 wiki lookup (WP-29) - the poe2-rag service's hybrid
retrieval (local embeddings + keyword search over the PoE2 wiki), without
its answer step: poe2-rag's /api/ask phrases with cloud Gemini, and Felt is
fully local, so this uses /api/search and hands the passages to her own
model to answer from.
"""

import httpx

from felt_core.plugin import SkillResult

name = "poe2_wiki"
description = (
    "Looks up Path of Exile 2 game knowledge in the PoE2 wiki: what a skill, "
    "support gem, unique item, keystone, passive, mechanic, boss or quest does, "
    "how something works, or whether something fits a build. Returns wiki "
    "passages for you to answer from. Use this for any PoE2 'what does X do' / "
    "'how does Y work' / 'is Z good' question - NOT web_search, and NOT "
    "poe_price_check (that one is only for what an item costs)."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The PoE2 question or term to look up"}
    },
    "required": ["query"],
}

_SEARCH_URL = "http://poe2rag:8000/api/search"
_TOP_K = 4
# The passages ride into one felt-llm follow-up (8K context, shared with
# the persona and tools) - keep them to a few thousand characters.
_PASSAGE_CHARS = 700


async def handle(params, ctx):
    query = params.get("query") or ctx.original_message
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(_SEARCH_URL, json={"query": query, "k": _TOP_K})
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
