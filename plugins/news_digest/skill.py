"""News/RSS digest — no vendor lock-in, just whatever feeds are
configured (FELT_NEWS_FEEDS, comma-separated). Empty by default; Mario
hasn't specified which feeds he actually wants yet, so this reports
itself unconfigured rather than guessing a default news source.

Can also check a one-off feed URL if the router extracts one from the
message (e.g. "what's new on <url>"), without needing it configured.
"""

import feedparser
import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "news_digest"
description = (
    "Headlines from a feed - only when the message contains a URL. Other news questions: "
    "web_search."
)
parameters_schema = {
    "type": "object",
    "properties": {"feed_url": {"type": "string", "description": "The feed URL"}},
}

_ENTRIES_PER_FEED = 5


async def _fetch_feed(client: httpx.AsyncClient, url: str) -> list[str]:
    try:
        response = await client.get(url, timeout=15, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError:
        return [f"(couldn't fetch {url})"]

    parsed = feedparser.parse(response.content)
    feed_title = parsed.feed.get("title", url)
    lines = [f"From {feed_title}:"]
    for entry in parsed.entries[:_ENTRIES_PER_FEED]:
        lines.append(f"- {entry.get('title', '(untitled)')}")
    return lines


async def handle(params, ctx):
    config = ctx.config
    one_off = get_single_param(params, "feed_url")
    feeds = [one_off] if one_off else config.news_feeds_list()

    if not feeds:
        return SkillResult(text="No news feeds configured yet — no default assumed.")

    async with httpx.AsyncClient() as client:
        all_lines: list[str] = []
        for feed_url in feeds:
            all_lines.extend(await _fetch_feed(client, feed_url))

    return SkillResult(text="\n".join(all_lines))
