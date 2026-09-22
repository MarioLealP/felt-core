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
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from felt_core import metrics, tts_cache
from felt_core.animation import resolve_animation_tag
from felt_core.cloud import cloud_generate
from felt_core.config import FeltConfig
from felt_core.db import (
    get_all_facts,
    get_current_screen_description,
    get_history,
    get_recent_observations,
    get_unresolved_pending_email,
    mark_email_resolved,
    save_message,
    save_observation,
    seconds_since_last_spoken_reaction,
    set_observation_reply,
)
from felt_core.email_watcher import fetch_email_body
from felt_core.plugin import ExecutionContext, UIAction
from felt_core.plugin_loader import load_plugins
from felt_core.privacy import is_safe_for_cloud
from felt_core.router import decide_tool
from felt_core.speech import strip_for_tts
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
    "personality.\n\n"
    "Everything you say is spoken aloud by a voice, never shown as text: "
    "no markdown, no lists, no emoji, no URLs; say numbers and units the "
    "way a person would out loud; lead with the answer."
)

# How much recent conversation rides along with a cloud-routed reply
# (2026-09-21, Mario: "we need some context window based on previous
# messages"). Bounded, not the full session - re-verified SAFE as a whole
# alongside the new message before it's allowed to reach the cloud (see
# _chat_reply), so this is real context, not an unbounded privacy surface.
CLOUD_CONTEXT_MESSAGES = 6

# Every reply is spoken aloud, not read - a long local-model reply means a
# long, rambly TTS clip (2026-09-22 finding: a weather reply's ~0.8 MB WAV,
# 12s of TTS alone out of a 33s turn). Ollama's own generation-length cap
# (num_predict), not a client-side truncation that could cut a sentence in
# half mid-word - the personality prompt's own "lead with the answer"
# instruction is what keeps a capped reply from feeling chopped off.
_REPLY_MAX_TOKENS = 160


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


# 2026-09-22 finding: "tell me something fun about octopuses" was routed by
# the small router (qwen2.5:1.5b) to generate_image - a real ComfyUI job got
# submitted, polled for ~48s, and the whole turn took 136s for a message
# with zero make-intent. generate_image has a real side effect (a GPU job)
# and a real cost (time), unlike every other skill here, so it gets a
# second, narrower check the router itself doesn't do: an explicit
# draw/make-an-image verb+noun pairing, not just topical similarity to
# "image" as a concept.
_IMAGE_INTENT_RE = re.compile(
    r"\b(draw|paint|sketch|generate|make|create|render|design)\b.*"
    r"\b(image|picture|pic|drawing|painting|art|portrait|wallpaper|illustration)\b"
    r"|\b(draw|paint|sketch)\s+(me|us|a|an|the)\b",
    re.IGNORECASE,
)


def _guard_generate_image(message: str) -> bool:
    return bool(_IMAGE_INTENT_RE.search(message))


# A table, not a single if-statement, so another expensive/side-effect
# skill can get its own guard later without touching chat()'s control flow.
_SKILL_GUARDS: dict[str, Callable[[str], bool]] = {
    "generate_image": _guard_generate_image,
}


async def _summarize_pending_email(config: FeltConfig, session_id: str, message: str) -> str:
    pending = get_unresolved_pending_email(config.db_path)
    assert pending is not None  # only called when the caller already confirmed this
    body = await asyncio.to_thread(fetch_email_body, config, pending["uid"])
    prompt = (
        f"Summarize this email from {pending['sender']} "
        f"(subject: {pending['subject']}) in 2-3 sentences, "
        f"conversationally, as if telling a friend what it says:\n\n{body[:6000]}"
    )

    # google_ok (plugins/skills.json: check_email) - straight to cloud, no
    # privacy check, WP-06 (Mario: "Google already has my email"). Falls
    # back to the existing local path on CloudUnavailable or any other
    # cloud error, same as every other cloud call site.
    summary = None
    if config.gemini_api_key:
        try:
            async with metrics.timed("reply", route="cloud", model=config.cloud_model_conversation):
                summary = await cloud_generate("summary", _PERSONALITY_PROMPT, [{"role": "user", "content": prompt}], config)
        except Exception:
            logger.exception("cloud email summary failed, falling back to local")
    if summary is None:
        async with metrics.timed("reply", route="local", model=config.ollama_model):
            summary = await _ollama_chat([{"role": "user", "content": prompt}], config.ollama_model, config)

    mark_email_resolved(config.db_path, pending["id"])
    save_message(config.db_path, session_id, "user", message)
    save_message(config.db_path, session_id, "assistant", summary)
    return summary


async def _ollama_chat(
    messages: list[dict], model: str, config: FeltConfig, *, max_tokens: int | None = None
) -> str:
    payload: dict = {"model": model, "messages": messages, "stream": False}
    if max_tokens is not None:
        # Replies are spoken aloud, not read - a long, rambly reply means
        # a long, rambly TTS clip (2026-09-22 finding: a weather reply's
        # ~0.8 MB WAV, 12s of TTS alone out of a 33s turn). num_predict is
        # Ollama's own generation-length cap, not a client-side truncation
        # that could cut a sentence in half mid-word.
        payload["options"] = {"num_predict": max_tokens}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(f"{config.ollama_url}/api/chat", json=payload)
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
    """Routes through cloud.cloud_generate's "conversation" purpose
    (WP-05) - NOT ninerouter/Antigravity, which contaminates every reply
    with its own coding-agent identity no matter what system prompt is
    supplied (see config.py's gemini_api_key comment). Carries a bounded
    recent-context window (CLOUD_CONTEXT_MESSAGES) and the personality
    prompt - never _facts_context (personal information Mario has
    explicitly asked Felt to remember), regardless of what the privacy
    check decided, since that's not what was classified. Raises
    CloudUnavailable (via cloud_generate) when the conversation/overflow
    models are both exhausted or unconfigured - callers already catch
    Exception broadly and fall back to local, so no new handling needed
    at the call sites."""
    recent = history[-CLOUD_CONTEXT_MESSAGES:]
    return await cloud_generate(
        "conversation", _PERSONALITY_PROMPT, recent + [{"role": "user", "content": message}], config
    )


def _facts_context(config: FeltConfig) -> list[dict]:
    facts = get_all_facts(config.db_path)
    if not facts:
        return []
    bullet_list = "\n".join(f"- {fact}" for fact in facts)
    return [{"role": "system", "content": f"Known facts about the user:\n{bullet_list}"}]


async def _local_reply(messages_tail: list[dict], config: FeltConfig, *, include_facts: bool) -> str:
    """The local (Ollama) reply path, factored out of three near-identical
    call sites (2026-09-22, WP-02) - plain chat's fallback, and both of
    screen-context's local branches. Always carries the personality
    prompt; facts (Mario's remembered personal info) are opt-in per
    caller, since screen-reaction replies don't need them (WP-02: "Facts
    are not needed; don't include them")."""
    facts = _facts_context(config) if include_facts else []
    messages = [{"role": "system", "content": _PERSONALITY_PROMPT}] + facts + messages_tail
    return await _ollama_chat(messages, config.ollama_model, config, max_tokens=_REPLY_MAX_TOKENS)


async def _chat_reply(message: str, history: list[dict], config: FeltConfig, safe: bool | None) -> str:
    """The actual reply-generation step for plain conversation (no skill
    involved) - routes to the cloud only when a Gemini API key is
    configured AND `safe` (the privacy classifier's verdict on the recent
    window + this message as a whole) is True; anything else
    (unconfigured, PRIVATE, or the cloud call itself failing) falls back
    to the existing local path, facts included, unchanged.

    `safe` is precomputed by the caller (chat()), started as a task
    running concurrently with the router call rather than after it
    (2026-09-22 finding: both are round trips to the Nitro5, ~3.8-4.9s for
    the router alone - serial meant paying both costs on every plain-chat
    turn with no skill involved). None means "not checked" (no Gemini key
    configured) - never treated as safe."""
    if config.gemini_api_key and safe:
        try:
            async with metrics.timed("reply", route="cloud", model=config.cloud_model_conversation):
                reply = await _cloud_chat(message, history, config)
            logger.info("chat reply: routed to cloud (%s)", config.cloud_model_conversation)
            return reply
        except Exception:
            logger.exception("cloud chat call failed, falling back to local")
    logger.info("chat reply: local (%s)", config.ollama_model)
    async with metrics.timed("reply", route="local", model=config.ollama_model):
        return await _local_reply(
            history + [{"role": "user", "content": message}], config, include_facts=True
        )


async def _timed_privacy_check(text: str, config: FeltConfig) -> bool:
    async with metrics.timed("privacy"):
        return await is_safe_for_cloud(text, config)


def _tool_result_instruction(tool_output: str) -> str:
    """Shared by every phrasing call site (the normal skill-reply path,
    skill_phrasing's cloud branch, and the weather precompute) so they all
    give the model the identical instruction, not three copies that could
    drift apart."""
    return (
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
        "Celsius, or any other unit; Mario already knows it's Celsius.\n\n"
        "Answer in one or two short spoken sentences; then, only if it fits, "
        "one short bit of personality (an opinion, a suggestion)."
    )


# Cloud skill-phrasing replies get a tighter cap than a normal local reply
# (_REPLY_MAX_TOKENS=160) - the WP-06 spec's own number, and Gemma/Gemini
# are both fast and cheap enough on the free tier that a slightly shorter
# ceiling costs nothing while keeping these replies snappy.
_SKILL_PHRASING_MAX_TOKENS = 120


async def _phrase_skill_result(
    message: str,
    tool_output: str,
    history: list[dict],
    base: list[dict],
    skill_privacy: str,
    config: FeltConfig,
) -> str:
    """Routes a skill's result to cloud or local phrasing by its privacy
    class (plugins/skills.json, WP-06, Mario 2026-09-22: "fetching data
    always stays local; the result text may be phrased in the cloud"):
    public/google_ok skills go straight to cloud; gated skills run the
    tool OUTPUT itself through is_safe_for_cloud first, cloud only if it
    passes; local skills never call cloud_generate at all - not even a
    classifier call, so there's no path from a "local" skill to the cloud
    even if that text would otherwise read as SAFE. Falls back to the
    existing local path (facts included, unchanged) on CloudUnavailable
    or any other cloud error."""
    route_cloud = skill_privacy in ("public", "google_ok")
    if skill_privacy == "gated":
        route_cloud = await is_safe_for_cloud(tool_output, config)
        logger.info(
            "skill phrasing: gated result %s the cloud privacy check",
            "passed" if route_cloud else "failed",
        )

    if route_cloud and config.gemini_api_key:
        try:
            # The tool result is already trusted for this skill's class,
            # but recent chat HISTORY might not be about the same thing at
            # all - re-verified safe as its own window before riding along
            # (same "don't just trust the new message alone" rule as
            # _chat_reply's cloud gate), and simply dropped (not blocked)
            # if it doesn't pass, since the tool result itself is still
            # fine to phrase in the cloud either way.
            history_window = history[-CLOUD_CONTEXT_MESSAGES:]
            if history_window and not await is_safe_for_cloud(
                _format_for_privacy_check(history_window, message), config
            ):
                history_window = []
            messages = history_window + [
                {"role": "user", "content": message},
                {"role": "user", "content": _tool_result_instruction(tool_output)},
            ]
            async with metrics.timed("reply", route="cloud", model=config.cloud_model_volume):
                return await cloud_generate(
                    "skill_phrasing", _PERSONALITY_PROMPT, messages, config, max_tokens=_SKILL_PHRASING_MAX_TOKENS
                )
        except Exception:
            logger.exception("cloud skill phrasing failed, falling back to local")

    async with metrics.timed("reply", route="local", model=config.ollama_model):
        return await _ollama_chat(
            [{"role": "system", "content": _PERSONALITY_PROMPT}]
            + base
            + [
                {"role": "user", "content": message},
                {"role": "user", "content": _tool_result_instruction(tool_output)},
            ],
            config.ollama_model,
            config,
            max_tokens=_REPLY_MAX_TOKENS,
        )


# WP-06: "what's the weather right now?" measured 33s end to end (17.4s of
# it just local phrasing) - a background loop keeps a ready-to-serve
# answer warm so the common case skips the phrasing model AND TTS
# entirely. Module-level, not per-request state; refreshed by
# refresh_weather_precompute() (started from api.lifespan) every
# _WEATHER_PRECOMPUTE_INTERVAL_S, and read by chat()'s fast path below.
_WEATHER_PRECOMPUTE_INTERVAL_S = 15 * 60
_WEATHER_PRECOMPUTE_MAX_AGE_S = 20 * 60
_WEATHER_NOW_PHRASES = {"now", "right now", "currently", "current", ""}
_weather_precompute: dict | None = None


def _weather_precompute_fresh() -> dict | None:
    if _weather_precompute is None:
        return None
    if time.monotonic() - _weather_precompute["computed_at"] > _WEATHER_PRECOMPUTE_MAX_AGE_S:
        return None
    return _weather_precompute


async def refresh_weather_precompute(config: FeltConfig) -> None:
    """One full precompute pass: run get_weather("now") for real, phrase
    it (cloud, same as a live request would), pre-render its TTS, and warm
    the TTS cache with that exact text - so chat()'s fast path and
    api._synthesize_reply's cache lookup are naturally consistent (both
    key off the identical `line` string, no separate coordination)."""
    global _weather_precompute

    registry = load_plugins(config.plugins_dir)
    if "get_weather" not in registry:
        return

    ctx = ExecutionContext(config=config, original_message="what's the weather right now?")
    try:
        result = await run_skill("get_weather", {"day": "now"}, registry, ctx)
    except ValueError:
        logger.warning("weather precompute: get_weather isn't registered, skipping")
        return
    tool_output = result.voice_line or result.text or ""

    line = tool_output
    if config.gemini_api_key:
        try:
            line = await cloud_generate(
                "skill_phrasing",
                _PERSONALITY_PROMPT,
                [
                    {"role": "user", "content": "what's the weather right now?"},
                    {"role": "user", "content": _tool_result_instruction(tool_output)},
                ],
                config,
                max_tokens=_SKILL_PHRASING_MAX_TOKENS,
            )
        except Exception:
            logger.exception("weather precompute: cloud phrasing failed, using the raw tool output")

    if config.voice_url:
        try:
            tts_text = strip_for_tts(line)
            async with httpx.AsyncClient(timeout=30) as client:
                tts_resp = await client.post(f"{config.voice_url}/v1/tts", json={"text": tts_text})
                tts_resp.raise_for_status()
            tts_cache.put(tts_text, tts_resp.content)
        except Exception:
            logger.exception("weather precompute: TTS pre-render failed")

    _weather_precompute = {
        "line": line,
        "animation_tag": result.animation_tag,
        "computed_at": time.monotonic(),
    }
    logger.info("weather precompute refreshed: %r", line)


async def weather_precompute_loop(config: FeltConfig) -> None:
    while True:
        try:
            await refresh_weather_precompute(config)
        except Exception:
            logger.exception("weather precompute loop iteration failed")
        await asyncio.sleep(_WEATHER_PRECOMPUTE_INTERVAL_S)


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

    # WP-02: "what do you think of this?" needs something to refer to.
    # `description` is already the privacy-filtered artifact (felt-vision's
    # own anti-PII system prompt runs before it ever reaches felt-core), so
    # this is safe to include on both the local and cloud paths, same as
    # the rest of `history` below. Only when fresh enough to still be
    # "current" - a stale observation would be a stale, misleading answer.
    current_screen = get_current_screen_description(config.db_path, session_id, max_age_seconds=120)
    if current_screen:
        history = history + [
            {"role": "system", "content": f"Currently on Mario's screen: {current_screen}"}
        ]

    base = facts + history
    save_message(config.db_path, session_id, "user", message)

    # Router and privacy check run concurrently, not serially (2026-09-22
    # finding: both are round trips to the Nitro5 - the router alone
    # measured ~3.8-4.9s - so a plain-chat turn with no skill involved was
    # paying both costs back to back for no reason). Only actually needed
    # when a skill DOESN'T end up getting called, but starting it now and
    # discarding it on a skill path is cheaper than a second round trip on
    # the far more common no-skill path.
    privacy_task = (
        asyncio.create_task(_timed_privacy_check(_format_for_privacy_check(history, message), config))
        if config.gemini_api_key
        else None
    )
    async with metrics.timed("router"):
        call = await decide_tool(message, registry, config)

    if call is not None:
        guard_skill_name, _ = call
        guard = _SKILL_GUARDS.get(guard_skill_name)
        if guard is not None and not guard(message):
            logger.info(
                "skill guard: overriding router's '%s' pick - no matching intent in message",
                guard_skill_name,
            )
            call = None

    # WP-06 fast path: the router already did the real classification work
    # (deciding this IS a "weather right now" question) - if a fresh
    # precomputed answer exists, skip run_skill AND the phrasing model
    # entirely and hand back that exact line, so api._synthesize_reply's
    # TTS cache (keyed on that same text, warmed by the precompute) is a
    # guaranteed hit too.
    if call is not None and call[0] == "get_weather":
        day = str(call[1].get("day", "now") or "now").lower()
        if day in _WEATHER_NOW_PHRASES:
            precompute = _weather_precompute_fresh()
            if precompute is not None:
                if privacy_task is not None:
                    privacy_task.cancel()
                    try:
                        await privacy_task
                    except asyncio.CancelledError:
                        pass
                logger.info("weather fast path: serving the precomputed line, no skill/phrasing calls")
                save_message(config.db_path, session_id, "assistant", precompute["line"])
                return ChatResult(reply=precompute["line"], animation_tag=precompute["animation_tag"])

    if call is None:
        # Fail-closed exactly like is_safe_for_cloud's own internal catch
        # (privacy.py): a broken/cancelled task must never read as "safe".
        safe: bool | None = None
        if privacy_task is not None:
            try:
                safe = await privacy_task
            except Exception:
                safe = False
        reply = await _chat_reply(message, history, config, safe)
        save_message(config.db_path, session_id, "assistant", reply)
        return ChatResult(reply=reply)

    if privacy_task is not None:
        privacy_task.cancel()
        try:
            await privacy_task
        except asyncio.CancelledError:
            pass

    skill_name, params = call
    # None (not the ValueError branch's job alone) so the animation/ui_action
    # lookup below has something safe to check regardless of which path
    # was taken - a skill that raised never gets a reaction, same as
    # before this change, just made explicit rather than an implicit
    # leftover variable from a previous call.
    result = None
    try:
        ctx = ExecutionContext(config=config, original_message=message)
        async with metrics.timed("skill", skill=skill_name):
            result = await run_skill(skill_name, params, registry, ctx)
        # voice_line (if the skill set one) over the full text - WP-06:
        # it's already the spoken-form summary, `text` is the fuller
        # toast/chat form (see plugin.py's SkillResult).
        tool_output = result.voice_line or result.text or ""
    except ValueError as e:
        tool_output = f"Error: {e}"

    skill_privacy = registry[skill_name].privacy if skill_name in registry else "local"
    reply = await _phrase_skill_result(message, tool_output, history, base, skill_privacy, config)
    save_message(config.db_path, session_id, "assistant", reply)

    if result is None:
        return ChatResult(reply=reply)
    return ChatResult(
        reply=reply,
        ui_action=result.ui_action,
        animation_tag=resolve_animation_tag(registry.get(skill_name), result),
    )


# No NONE option (2026-09-22, WP-02, Mario's rule: screen reactions are
# always spoken - frequency is a deterministic cooldown, never the model
# choosing silence). Previous prompt let the model answer NONE, and on the
# cloud path Gemini said NONE 11/11 times - she effectively never reacted.
_SCREEN_REACTION_PROMPT = (
    "You just glanced at Mario's screen. Say ONE short, natural spoken remark "
    "about it (max ~15 words), in character. Never say you're looking at a "
    "screen or describe it neutrally. React like a friend sitting next to him."
)


def _observations_block(observations: list[dict]) -> str:
    lines = "\n".join(f"- {obs['description']}" for obs in observations)
    return f"Recently on screen (oldest first):\n{lines}"


async def process_screen_context(description: str, config: FeltConfig, session_id: str = "default") -> ChatResult:
    """Handles passive screen context from felt-vision - always produces a
    spoken reaction (Mario's rule), rate-limited by a deterministic
    cooldown rather than the model deciding to stay silent.

    2026-09-22 fix: this used to have the model invent its own animation tag
    vocabulary (bracket tags like [nod], [look_bored], [lean_in]) for the
    silent case. None of those exist anywhere in the real animation
    library (felt-animation's actual tags look like "gesture/nodding",
    "emotion/happy", scanned from disk - see felt-animation/library.py) -
    felt-core (which runs on the server) has no way to know that real set,
    it's desktop-local state. Fixed by never inventing a tag here - always
    animation_tag=None, so felt-animation's own classify_reaction()
    (already proven, already validated against the real known_tags) picks
    it downstream instead.

    WP-02 (2026-09-22): observations moved to their own table
    (screen_observations, never `messages`) - saving one as a fake `user`
    message wiped real conversation out of get_history's 20-row window
    after ~20 window switches, and made every observation show up as a
    fake "Mario said" bubble in the dashboard chat.
    """
    obs_id = save_observation(config.db_path, session_id, description)

    seconds_since = seconds_since_last_spoken_reaction(config.db_path, session_id)
    if seconds_since is not None and seconds_since < config.screen_reaction_cooldown_s:
        logger.info(
            "screen reaction: on cooldown (%.0fs since last spoken reaction, need %ds) - no model call",
            seconds_since,
            config.screen_reaction_cooldown_s,
        )
        return ChatResult(reply="", animation_tag=None)

    observations_block = _observations_block(get_recent_observations(config.db_path, session_id, limit=3))

    async def _local_attempt() -> str:
        # Last 4 REAL chat messages, not facts - facts are personal-info
        # recall, irrelevant to reacting to what's on screen right now.
        history_tail = get_history(config.db_path, session_id)[-4:]
        async with metrics.timed("screen_reaction", route="local", model=config.ollama_model):
            return await _local_reply(
                [{"role": "system", "content": observations_block}]
                + history_tail
                + [{"role": "user", "content": _SCREEN_REACTION_PROMPT}],
                config,
                include_facts=False,
            )

    if config.gemini_api_key and await is_safe_for_cloud(description, config):
        try:
            logger.info("screen context reaction: routed to cloud")
            # No chat history at all on the cloud path (WP-02) - only
            # personality + the observations block go in `system`, and the
            # prompt is the sole message; WP-05's cloud_generate only
            # accepts user/assistant roles in `messages`, so what used to
            # be a "system"-tagged history entry now lives in `system`
            # instead. Routes through the "screen_reaction" purpose (the
            # volume/Gemma model), not "conversation".
            reply_raw = await cloud_generate(
                "screen_reaction",
                f"{_PERSONALITY_PROMPT}\n\n{observations_block}",
                [{"role": "user", "content": _SCREEN_REACTION_PROMPT}],
                config,
            )
        except Exception:
            logger.exception("cloud screen context failed, falling back to local")
            reply_raw = await _local_attempt()
    else:
        logger.info("screen context reaction: local")
        reply_raw = await _local_attempt()

    reply_raw = reply_raw.strip(" \n\"'")

    # A legacy habit (the old NONE-option prompt) some models still fall
    # back to even with the new prompt - retried once, locally, rather than
    # silently accepted, so a transient bad cloud reply doesn't just vanish
    # a reaction that Mario's rule says must be spoken.
    if not reply_raw or reply_raw.upper() == "NONE":
        logger.info("screen reaction empty/NONE, retrying once on the local path")
        reply_raw = (await _local_attempt()).strip(" \n\"'")
        if not reply_raw or reply_raw.upper() == "NONE":
            logger.warning("screen reaction still empty after local retry - returning silent")
            return ChatResult(reply="", animation_tag=None)

    set_observation_reply(config.db_path, obs_id, reply_raw)
    save_message(config.db_path, session_id, "assistant", reply_raw)
    return ChatResult(reply=reply_raw, animation_tag=None)
