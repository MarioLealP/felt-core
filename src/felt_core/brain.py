"""The brain loop: a small router decides tool-vs-chat, the main model
only ever converses. Now with real conversation memory (2026-09-11) — a
session's prior messages are loaded from SQLite and included, so a
follow-up question actually has context.

2026-09-11 redesign (kept from before): routing is a separate, narrow
classification call to a small dedicated model (config.router_model); the
main model (config.ollama_model, chosen for chat quality) never sees
tool-call instructions at all, it just answers.
"""

import asyncio
import re

import httpx

from felt_core.config import FeltConfig
from felt_core.db import (
    get_all_facts,
    get_history,
    get_unresolved_pending_email,
    mark_email_resolved,
    save_message,
)
from felt_core.email_watcher import fetch_email_body
from felt_core.plugin import ExecutionContext
from felt_core.plugin_loader import load_plugins
from felt_core.router import decide_tool
from felt_core.tools import run_skill

# Deliberately a plain keyword set, not another router call: a bare "yes"
# has no content for a tool-vs-chat classifier to work with (the router
# never sees conversation history, only the current message — see
# router.py), and this only ever fires when a pending_emails row already
# says a confirmation is being waited on, so the blast radius of a false
# positive is small (summarizing an email Mario didn't actually ask
# about) compared to the complexity of a more "proper" intent check.
_AFFIRMATIVE_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|please|do it|summarize( it)?)[.!]?\s*$",
    re.IGNORECASE,
)


def _looks_affirmative(message: str) -> bool:
    return bool(_AFFIRMATIVE_RE.match(message))


async def _summarize_pending_email(config: FeltConfig, session_id: str, message: str) -> str:
    pending = get_unresolved_pending_email(config.db_path)
    assert pending is not None  # only called when the caller already confirmed this
    body = await asyncio.to_thread(fetch_email_body, config, pending["uid"])
    summary = await _ollama_chat(
        [
            {
                "role": "user",
                "content": (
                    f"Summarize this email from {pending['sender']} "
                    f"(subject: {pending['subject']}) in 2-3 sentences, "
                    f"conversationally, as if telling a friend what it says:\n\n{body[:6000]}"
                ),
            }
        ],
        config.ollama_model,
        config,
    )
    mark_email_resolved(config.db_path, pending["id"])
    save_message(config.db_path, session_id, "user", message)
    save_message(config.db_path, session_id, "assistant", summary)
    return summary


async def _ollama_chat(messages: list[dict], model: str, config: FeltConfig) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{config.ollama_url}/api/chat",
            json={"model": model, "messages": messages, "stream": False},
        )
        response.raise_for_status()
        return response.json()["message"]["content"]


def _facts_context(config: FeltConfig) -> list[dict]:
    facts = get_all_facts(config.db_path)
    if not facts:
        return []
    bullet_list = "\n".join(f"- {fact}" for fact in facts)
    return [{"role": "system", "content": f"Known facts about the user:\n{bullet_list}"}]


async def chat(message: str, config: FeltConfig, session_id: str = "default") -> str:
    # Checked before anything else, including history/facts loading — a
    # bare "yes" answering the proactive "want me to summarize it?" ping
    # (email_watcher.py) has to be caught here or it falls through to the
    # router with zero context and either misroutes or gets a generic
    # chat reply that doesn't know any email exists.
    if _looks_affirmative(message) and get_unresolved_pending_email(config.db_path):
        return await _summarize_pending_email(config, session_id, message)

    registry = load_plugins(config.plugins_dir)
    base = _facts_context(config) + get_history(config.db_path, session_id)
    save_message(config.db_path, session_id, "user", message)

    call = await decide_tool(message, registry, config)

    if call is None:
        reply = await _ollama_chat(
            base + [{"role": "user", "content": message}], config.ollama_model, config
        )
        save_message(config.db_path, session_id, "assistant", reply)
        return reply

    skill_name, params = call
    try:
        ctx = ExecutionContext(config=config, original_message=message)
        result = await run_skill(skill_name, params, registry, ctx)
        tool_output = result.text or ""
    except ValueError as e:
        tool_output = f"Error: {e}"

    reply = await _ollama_chat(
        base
        + [
            {"role": "user", "content": message},
            {
                "role": "user",
                "content": (
                    f"(Tool result: {tool_output})\n\n"
                    "State that fact directly and naturally, as if you already knew it. "
                    "Do not mention tools, and do not add any disclaimer about lacking "
                    "real-time access — the tool result above IS current and accurate. "
                    "If the tool result says something wasn't found or an error occurred, "
                    "relay that honestly — do NOT invent an unrelated answer, platform, "
                    "reason, or context that isn't literally in the tool result; a correct "
                    "'couldn't find that' is always better than a confident wrong guess. "
                    "If the tool result gives a temperature as a bare number with a degree "
                    "sign (e.g. '15°'), relay it exactly as-is — do NOT append C, °C, "
                    "Celsius, or any other unit; Mario already knows it's Celsius."
                ),
            },
        ],
        config.ollama_model,
        config,
    )
    save_message(config.db_path, session_id, "assistant", reply)
    return reply
