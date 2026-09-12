"""General web search — self-hosted SearXNG, no API key, no vendor
lock-in. Returns raw result snippets for the LOCAL chat model to read and
answer from, unlike delegate_research: no cloud LLM call happens here at
all, just a search engine fetch. Stays local-first — only reaches for
delegate_research when the question needs actual synthesis/reasoning a
snippet list can't provide.
"""

import httpx

from felt_core.plugin import SkillResult

name = "web_search"
description = (
    "Searches the web for raw results/snippets on a general-knowledge topic "
    "and returns them for you to read and answer from yourself. Stays fully "
    "local (self-hosted search, no cloud LLM call) — use this first for "
    "straightforward lookups. NOT the same as delegate_research (which asks "
    "a cloud model to actually reason/synthesize) — only escalate to that if "
    "these snippets genuinely aren't enough. NOT for weather (use "
    "get_weather instead) or for Felt's own alerts (use get_recent_alerts). "
    "For a general 'what's the news' or 'top headline' question with no "
    "specific feed URL mentioned, use THIS, not news_digest — news_digest "
    "is only for a specific, already-known feed URL."
)
parameters_schema = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "What to search for"}},
    "required": ["query"],
}

_SEARXNG_URL = "http://searxng:8080/search"


async def handle(params, ctx):
    query = params.get("query") or ctx.original_message
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(_SEARXNG_URL, params={"q": query, "format": "json"})
        response.raise_for_status()
        results = response.json().get("results", [])[:5]

    if not results:
        return SkillResult(text="No search results found.")

    lines = [f"- {r['title']}: {r.get('content', '')}" for r in results]
    return SkillResult(text="\n".join(lines))
