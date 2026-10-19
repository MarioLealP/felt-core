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
    "Searches the web and returns snippets to answer from. Not for weather (get_weather),"
    " Felt's alerts (get_recent_alerts) or a feed URL (news_digest)."
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
