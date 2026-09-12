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
from datetime import date

import httpx

from felt_core.config import FeltConfig
from felt_core.plugin_loader import LoadedSkill

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _build_router_prompt(registry: dict[str, LoadedSkill], today: date | None = None) -> str:
    tool_lines = "\n".join(
        f"- {skill.name}: {skill.description}" for skill in registry.values()
    )
    today = today or date.today()
    return (
        "You are a routing classifier, not a conversational assistant. "
        f"For reference, today's date is {today.isoformat()}.\n"
        "Given the user's message, decide whether one of these tools "
        "should be called to answer it:\n"
        f"{tool_lines}\n\n"
        "IMPORTANT: if more than one tool could technically apply, always "
        "pick the most SPECIFIC one over a general-purpose one (e.g. "
        "web_search). Worked examples:\n"
        'User: "What\'s the weather like right now?"\n'
        'Correct: {"skill": "get_weather", "params": {}}\n'
        'Wrong: {"skill": "web_search", "params": {"query": "weather"}} '
        "— wrong because a dedicated weather tool exists.\n\n"
        'User: "What\'s new on news.ycombinator.com/rss?"\n'
        'Correct: {"skill": "news_digest", "params": {"feed_url": "news.ycombinator.com/rss"}}\n'
        'Wrong: {"skill": "web_search", "params": {"query": "news.ycombinator.com/rss"}} '
        "— wrong because the message contains an actual feed URL, which "
        "news_digest handles directly.\n\n"
        'User: "Search the web for today\'s top news headline."\n'
        'Correct: {"skill": "web_search", "params": {"query": "top news headline"}}\n'
        'Wrong: {"skill": "news_digest", "params": {"feed_url": "..."}} — '
        "wrong because this message has NO URL in it at all. Never invent "
        "or reuse a feed_url from an example — only use news_digest when "
        "the CURRENT message literally contains a URL.\n\n"
        "MECHANICAL RULE, overrides wording/topic similarity: scan the "
        "CURRENT message character-by-character for 'http' or a domain "
        "pattern (word.word/ or word.word, e.g. 'site.com'). Found one? "
        "news_digest, always, no matter how the rest of the sentence is "
        "phrased. Found none? Never news_digest.\n\n"
        'User: "What\'s the weather in 5 days?"\n'
        'Correct: {"skill": "get_weather", "params": {"day": "in 5 days"}}\n'
        'Wrong: {"skill": "get_weather", "params": {"days": 5}} — wrong '
        "param key (must be \"day\"), and wrong to extract just the "
        "number out of the phrase.\n\n"
        'User: "What\'s the weather on october 3rd?"\n'
        'Correct: {"skill": "get_weather", "params": {"day": "october 3rd"}}\n'
        'Wrong: {"skill": "get_weather", "params": {"date": "2023-10-03"}} — '
        "wrong to invent a year that isn't in the message.\n\n"
        "RULE for get_weather's day/date argument: always pass the "
        "phrase through EXACTLY as the user said it, under the key "
        '"day". Never reformat it into ISO format, never invent a year, '
        "never reduce a phrase like \"in 5 days\" to just the number.\n\n"
        'If a tool clearly applies, output exactly: {"skill": "<name>", "params": {...}}\n'
        'If no tool is needed, output exactly: {"skill": null}\n'
        "Output ONLY that JSON object. No explanation, no apology, "
        "no other text."
    )


def parse_router_response(raw: str) -> tuple[str, dict] | None:
    """Pure parsing logic, split out from decide_tool so it's unit-
    testable without a live model call."""
    match = _JSON_RE.search(raw)
    if not match:
        return None
    try:
        call = json.loads(match.group(0))
    except json.JSONDecodeError:
        # 2026-09-12 finding: the router occasionally emits a stray
        # quote right before a closing brace/bracket, e.g.
        # {"skill": "x", "params": {}"} — a genuine decision, just
        # malformed JSON. Try one targeted repair before giving up.
        #
        # Deliberately narrow: only strips a quote sandwiched BETWEEN two
        # structural brackets ([}\]]"[}\]]), never one merely followed by
        # a bracket — an earlier, broader version of this regex would
        # have also eaten the legitimate closing quote of any string
        # value that happens to end right before a brace (e.g.
        # {"item": "Redbeak"} — a real case a test caught), corrupting
        # otherwise-valid JSON instead of only fixing the actual bug.
        repaired = re.sub(r'([}\]])"\s*([}\]])', r"\1\2", match.group(0))
        try:
            call = json.loads(repaired)
        except json.JSONDecodeError:
            return None

    skill_name = call.get("skill")
    if not skill_name:
        return None
    return skill_name, call.get("params", {})


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

    return parse_router_response(raw)
