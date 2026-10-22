"""Mario's news (2026-10-22): the stories on his Home Assistant news sensors.

A server job (infra ops/sbin/clockmost-news-feeds.py; workspace
runbooks/news-feeds.md) fetches his sources every hour, has Bonsai rate and
summarize each story, and keeps the few that matter per topic in
`sensor.news_<topic>` (attribute `stories`). The dashboard shows the same
ones. She reads them with her own HA token and tells him the ones worth
hearing - no feed fetching of her own (Mario: "have her pick from the pool").
Stories are in their own language (mostly Portuguese); she answers in his.
"""

import re
from datetime import datetime

import httpx

from felt_core.ha_calendar import TZ
from felt_core.plugin import SkillResult

name = "news_digest"
description = (
    "Today's news from Mario's own feeds: Portugal (politics), Greater Porto (where he lives), "
    "World (big events only), JN Kids (science), his games (FFXIV, Path of Exile, Endfield, "
    "WoW Forever) and tech. For 'what's the news', 'anything new in X'. Something specific "
    "that isn't there: web_search."
)
TOPICS = {
    "portugal": "Portugal",
    "porto": "Greater Porto",
    "world": "World",
    "jn_kids": "JN Kids (science)",
    "games": "Games",
    "tech": "Tech",
}
parameters_schema = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "description": "One topic, or all (default)",
            "enum": [*TOPICS, "all"],
        }
    },
}


# his message in Portuguese: accents or a few everyday words ("há novidades no Porto?")
_PT = re.compile(
    r"[ãõçáéíóúâêô]|\b(?:que|há|notícias|noticias|novidades|hoje|algo|sobre|jogos|o que)\b",
    re.IGNORECASE,
)


def reply_language(message: str) -> str:
    """Pure: the language she answers in - his message's, never the stories'."""
    return "Portuguese from Portugal" if _PT.search(message or "") else "English"


def when(published: float | None, now: datetime) -> str:
    """Pure: 'today 14:05' / 'yesterday 09:30' / 'Monday 19 October' for a story's time."""
    if not published:
        return ""
    at = datetime.fromtimestamp(published, TZ)
    days = (now.date() - at.date()).days
    if days == 0:
        return f"today {at:%H:%M}"
    if days == 1:
        return f"yesterday {at:%H:%M}"
    return f"{at:%A} {at.day} {at:%B}"  # the date: "Sunday" alone made a past test "tomorrow"


def topic_lines(label: str, stories: list[dict], now: datetime) -> list[str]:
    """Pure: one topic's stories as lines for her model."""
    if not stories:
        return [f"{label}: nothing new."]
    lines = [f"{label}:"]
    for s in stories:
        meta = ", ".join(x for x in (s.get("source"), when(s.get("published"), now)) if x)
        text = s.get("digest") or s.get("summary") or ""
        lines.append(f"- {s.get('title', '')} ({meta})" + (f": {text}" if text else ""))
    return lines


async def handle(params, ctx):
    config = ctx.config
    if not (config.ha_url and config.ha_token):
        return SkillResult(text="The news isn't reachable - Home Assistant isn't connected.")
    topic = str(params.get("topic") or "all").lower()
    wanted = [topic] if topic in TOPICS else list(TOPICS)
    now = datetime.now(TZ)
    lines: list[str] = []
    headers = {"Authorization": f"Bearer {config.ha_token}"}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        for t in wanted:
            r = await client.get(f"{config.ha_url}/api/states/sensor.news_{t}")
            if r.status_code == 404:
                continue
            r.raise_for_status()
            stories = r.json()["attributes"].get("stories") or []
            # all topics: each one's top story only (ranked best first) - with them all she
            # led with world/tech and left Portugal + Porto out (e2e, 2 of 2)
            lines += topic_lines(TOPICS[t], stories[:1] if len(wanted) > 1 else stories, now)
    if not lines:
        return SkillResult(text="No news yet - the news sensors are empty.")
    # e2e 2026-10-22: a note at the end ("answer in his message's language") lost to a page
    # of Portuguese stories 3 times in 6 - the language named, first; and "what's the news"
    # gave only tech + games
    lang = reply_language(ctx.original_message)
    note = f"Answer Mario in {lang} - translate the stories (most are in Portuguese)."
    # games keep a week: "the stress test is on the 21st" was said on the 22nd (e2e)
    note += f" Today is {now:%A} {now.day} {now:%B} - a date before it is past."
    if len(wanted) > 1:
        note += " A quick round: one short line per topic, in this order."
    note += " The titles are the facts - keep what they say."
    return SkillResult(text=f"({note})\n" + "\n".join(lines))
