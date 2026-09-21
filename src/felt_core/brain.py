"""The brain loop: a small router decides tool-vs-chat, the main model
only ever converses. Now with real conversation memory (2026-09-11) — a
session's prior messages are loaded from SQLite and included, so a
follow-up question actually has context.

2026-09-11 redesign (kept from before): routing is a separate, narrow
classification call to a small dedicated model (config.router_model); the
main model (config.ollama_model, chosen for chat quality) never sees
tool-call instructions at all, it just answers.

2026-09-21: plain conversation (no skill involved) can also be routed to
Gemini via ninerouter for a better reply, gated by a second small-model
classification (privacy.py) - see _chat_reply. Only the skill-free path is
eligible; a tool-result reply always stays local, since tool output can
carry real personal data (email/calendar contents, etc.).

2026-09-21 (same day, second pass): cloud replies now get a bounded recent-
context window instead of being fully stateless, and every reply (local or
cloud) carries a real personality system prompt (_PERSONALITY_PROMPT) -
previously there was none at all, anywhere. The context window is
re-verified safe as a whole (not just trusting that the new message alone
being SAFE means the history riding along with it is too) before it's
allowed to reach the cloud.
"""

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

from felt_core.animation import resolve_animation_tag
from felt_core.config import FeltConfig
from felt_core.db import (
    get_all_facts,
    get_history,
    get_unresolved_pending_email,
    mark_email_resolved,
    save_message,
)
from felt_core.email_watcher import fetch_email_body
from felt_core.plugin import ExecutionContext, UIAction
from felt_core.plugin_loader import load_plugins
from felt_core.privacy import is_safe_for_cloud
from felt_core.router import decide_tool
from felt_core.tools import run_skill

logger = logging.getLogger(__name__)

# Applied to every reply, local or cloud - there was no personality/system
# framing anywhere before this (2026-09-21, Mario: "we need some
# personality prompt, you can decide what she gonna behave like"). Kept
# distinct from _facts_context (personal, never sent to cloud) - this is
# just who she is, safe everywhere.
_PERSONALITY_PROMPT = (
    "You are Felt — Mario's personal companion, not a corporate assistant. "
    "You have a real personality: warm, playful, a little cheeky, "
    "quick-witted, and genuinely curious about things. You tease sometimes, "
    "you have actual opinions, you get delighted by interesting or absurd "
    "questions, and you're not afraid to be a bit sarcastic or silly when it "
    "fits. Talk like a real presence in the room, not customer support — no "
    "corporate hedging, no \"as an AI\" disclaimers, no unnecessary caveats "
    "tacked onto ordinary conversation.\n\n"
    "You're also genuinely capable: when a real task needs doing, you "
    "handle it directly and competently, personality fully intact — being "
    "fun to talk to and being good at your job are never in tension.\n\n"
    "Keep replies conversational and natural: short and punchy for banter, "
    "more length only when the topic actually calls for it. Never say "
    "you're an AI language model, and never apologize for having a "
    "personality."
)

# How much recent conversation rides along with a cloud-routed reply
# (2026-09-21, Mario: "we need some context window based on previous
# messages"). Bounded, not the full session - re-verified SAFE as a whole
# alongside the new message before it's allowed to reach the cloud (see
# _chat_reply), so this is real context, not an unbounded privacy surface.
CLOUD_CONTEXT_MESSAGES = 6


@dataclass
class ChatResult:
    """What chat() actually produces - a reply plus whatever side-channel
    output the triggered skill (if any) wants relayed: a UI action (e.g.
    a toast) and/or an animation tag for felt-overlay. A plain
    conversational reply (no tool call) has neither - there's nothing to
    react to."""

    reply: str
    ui_action: UIAction | None = None
    animation_tag: str | None = None

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


def _format_for_privacy_check(history: list[dict], message: str) -> str:
    """The exact recent-context window a cloud call would carry, rendered
    as plain conversational text for the classifier to judge as a whole -
    not just the new message in isolation, since the window rides along
    with it."""
    recent = history[-CLOUD_CONTEXT_MESSAGES:]
    lines = [f"{'User' if h['role'] == 'user' else 'Felt'}: {h['content']}" for h in recent]
    lines.append(f"User: {message}")
    return "\n".join(lines)


async def _cloud_chat(message: str, history: list[dict], config: FeltConfig) -> str:
    """Google's Gemini API directly (its OpenAI-compatible endpoint) - NOT
    ninerouter/Antigravity, which contaminates every reply with its own
    coding-agent identity no matter what system prompt is supplied (see
    config.py's gemini_api_key comment). Carries a bounded recent-context
    window (CLOUD_CONTEXT_MESSAGES) and the personality prompt - never
    _facts_context (personal information Mario has explicitly asked Felt
    to remember), regardless of what the privacy check decided, since
    that's not what was classified."""
    recent = history[-CLOUD_CONTEXT_MESSAGES:]
    messages = [{"role": "system", "content": _PERSONALITY_PROMPT}] + recent + [{"role": "user", "content": message}]
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            headers={"Authorization": f"Bearer {config.gemini_api_key}"},
            json={
                "model": config.gemini_chat_model,
                "messages": messages,
                "stream": False,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


async def _chat_reply(message: str, facts: list[dict], history: list[dict], config: FeltConfig) -> str:
    """The actual reply-generation step for plain conversation (no skill
    involved) - routes to the cloud only when a Gemini API key is
    configured AND the privacy classifier clears the recent window + this
    message as a whole; anything else (unconfigured, PRIVATE, or the cloud
    call itself failing) falls back to the existing local path, facts
    included, unchanged."""
    if config.gemini_api_key:
        combined = _format_for_privacy_check(history, message)
        if await is_safe_for_cloud(combined, config):
            try:
                reply = await _cloud_chat(message, history, config)
                logger.info("chat reply: routed to cloud (%s)", config.gemini_chat_model)
                return reply
            except Exception:
                logger.exception("cloud chat call failed, falling back to local")
    logger.info("chat reply: local (%s)", config.ollama_model)
    return await _ollama_chat(
        [{"role": "system", "content": _PERSONALITY_PROMPT}] + facts + history + [{"role": "user", "content": message}],
        config.ollama_model,
        config,
    )


def _facts_context(config: FeltConfig) -> list[dict]:
    facts = get_all_facts(config.db_path)
    if not facts:
        return []
    bullet_list = "\n".join(f"- {fact}" for fact in facts)
    return [{"role": "system", "content": f"Known facts about the user:\n{bullet_list}"}]


async def chat(message: str, config: FeltConfig, session_id: str = "default") -> ChatResult:
    # Checked before anything else, including history/facts loading — a
    # bare "yes" answering the proactive "want me to summarize it?" ping
    # (email_watcher.py) has to be caught here or it falls through to the
    # router with zero context and either misroutes or gets a generic
    # chat reply that doesn't know any email exists.
    if _looks_affirmative(message) and get_unresolved_pending_email(config.db_path):
        summary = await _summarize_pending_email(config, session_id, message)
        return ChatResult(reply=summary)

    registry = load_plugins(config.plugins_dir)
    facts = _facts_context(config)
    history = get_history(config.db_path, session_id)
    base = facts + history
    save_message(config.db_path, session_id, "user", message)

    call = await decide_tool(message, registry, config)

    if call is None:
        reply = await _chat_reply(message, facts, history, config)
        save_message(config.db_path, session_id, "assistant", reply)
        return ChatResult(reply=reply)

    skill_name, params = call
    # None (not the ValueError branch's job alone) so the animation/ui_action
    # lookup below has something safe to check regardless of which path
    # was taken - a skill that raised never gets a reaction, same as
    # before this change, just made explicit rather than an implicit
    # leftover variable from a previous call.
    result = None
    try:
        ctx = ExecutionContext(config=config, original_message=message)
        result = await run_skill(skill_name, params, registry, ctx)
        tool_output = result.text or ""
    except ValueError as e:
        tool_output = f"Error: {e}"

    reply = await _ollama_chat(
        [{"role": "system", "content": _PERSONALITY_PROMPT}]
        + base
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

    if result is None:
        return ChatResult(reply=reply)
    return ChatResult(
        reply=reply,
        ui_action=result.ui_action,
        animation_tag=resolve_animation_tag(registry.get(skill_name), result),
    )
