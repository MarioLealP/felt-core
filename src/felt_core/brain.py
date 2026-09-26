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
import json
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
    get_music_taste_stats,
    get_recent_observations,
    get_unresolved_pending_email,
    mark_email_resolved,
    record_music_play,
    save_message,
    save_observation,
    seconds_since_last_spoken_reaction,
    set_observation_reply,
)
from felt_core.email_watcher import fetch_email_body
from felt_core.music_taste import is_skip, summarize_taste
from felt_core.plugin import ExecutionContext, UIAction
from felt_core.plugin_loader import load_plugins
from felt_core.privacy import is_safe_for_cloud
from felt_core.router import decide_tool
from felt_core.speech import strip_for_tts
from felt_core.spotify_client import get_currently_playing
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


def _personality_prompt(tone: str | None = None) -> str:
    """WP-15: `tone` is felt-animation's GET /tone line (mood x time-of-day
    + streak awareness - felt-tray/felt-dashboard fetch it and attach it to
    /v1/react/screen, /v1/voice-chat, /v1/text-chat). None on an older
    client, felt-animation unreachable, or a call site that doesn't carry
    one (the pending-email summary, the weather precompute) - the base
    personality prompt alone is always a safe, complete system prompt on
    its own, this is purely additive."""
    if not tone:
        return _PERSONALITY_PROMPT
    return f"{_PERSONALITY_PROMPT}\n\nRight now: {tone}"

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
    # WP-07's screen_reaction JSON contract only - one of a fixed set
    # (happy/excited/amused/sad/annoyed/surprised/thinking/sleepy/neutral),
    # None everywhere else.
    emotion: str | None = None

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

# WP-09: "shut up" cuts her off mid-sentence - the whole point is that it's
# immediate and never gets second-guessed by a router model's own judgment
# call (or delayed by the round trip to it). Matched as a deterministic
# regex fast path, same shape as _looks_affirmative above, checked before
# decide_tool ever runs (see chat()) rather than left to the router's own
# worked example, which is only a fallback for phrasing this doesn't catch.
_HUSH_RE = re.compile(
    r"^\s*(shut up|shh+|be quiet|quiet please|stop talking)[.!]?\s*$", re.IGNORECASE
)
_UNHUSH_RE = re.compile(r"^\s*(you can talk again|ok(ay)? talk)[.!]?\s*$", re.IGNORECASE)


def _presence_fast_path(message: str) -> tuple[str, dict] | None:
    if _HUSH_RE.match(message):
        return "felt_presence", {"intent": "shut_up"}
    if _UNHUSH_RE.match(message):
        return "felt_presence", {"intent": "unhush"}
    return None


# Never-truly-quiet: the router LLM must not be able to hush her or send her
# away on a guess. Live 2026-09-25, "keep me company" was routed to
# felt_presence and came back as a hush (the skill's substring fallback maps
# any invented intent containing "quiet" to shut_up). A router-picked
# felt_presence call therefore only stands if its intent is one of the
# harmless ones below, or if Mario's own words explicitly ask for silence or
# distance. The hotkeys (Ctrl+Alt+S/H/B) remain the reliable path.
_ROUTER_SAFE_PRESENCE_INTENTS = {
    "unhush", "ok_talk", "you_can_talk_again",
    "come_back", "come", "come_here",
    "move", "move_over", "shoo",
    "look", "look_at_this",
    "repeat", "what_did_you_say", "say_that_again",
}
_EXPLICIT_SILENCE_REQUEST_RE = re.compile(
    r"\b(shut up|shh+|be quiet|quiet (please|down)|stop talking|zip it|go away|"
    r"hide|leave me alone|be right back|brb|give me (a |\w+ )?(minute|minutes|sec|second|moment))\b",
    re.IGNORECASE,
)


def _router_presence_allowed(message: str, params: dict) -> bool:
    intent = str(params.get("intent") or "").strip().lower().replace(" ", "_")
    if intent in _ROUTER_SAFE_PRESENCE_INTENTS:
        return True
    return bool(_EXPLICIT_SILENCE_REQUEST_RE.search(message))


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
        # Same shape as every other local reply (personality prompt + the
        # spoken-length cap) - it used to be a bare, uncapped prompt (audit
        # 2026-09-24). No facts: the email body is all the context needed.
        async with metrics.timed("reply", route="local", model=config.ollama_model):
            summary = await _local_reply([{"role": "user", "content": prompt}], config, include_facts=False)

    mark_email_resolved(config.db_path, pending["id"])
    save_message(config.db_path, session_id, "user", message)
    save_message(config.db_path, session_id, "assistant", summary)
    return summary


async def _ollama_chat(
    messages: list[dict], model: str, config: FeltConfig, *, max_tokens: int | None = None
) -> str:
    # num_ctx always explicit: the context size sets the KV-cache VRAM, and
    # the chat model shares the Nitro5's card with felt-voice (see config).
    options: dict = {"num_ctx": config.ollama_num_ctx}
    payload: dict = {"model": model, "messages": messages, "stream": False, "options": options}
    if max_tokens is not None:
        # Replies are spoken aloud, not read - a long, rambly reply means
        # a long, rambly TTS clip (2026-09-22 finding: a weather reply's
        # ~0.8 MB WAV, 12s of TTS alone out of a 33s turn). num_predict is
        # Ollama's own generation-length cap, not a client-side truncation
        # that could cut a sentence in half mid-word.
        options["num_predict"] = max_tokens
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


async def _cloud_chat(message: str, history: list[dict], config: FeltConfig, tone: str | None = None) -> str:
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
    # Music taste goes into the system instruction, not `messages` - the
    # Gemini API has no system role there, so a "system" entry would be
    # sent as a user turn (audit 2026-09-24).
    system = _personality_prompt(tone)
    for note in _music_taste_context(config):
        system = f"{system}\n\n{note['content']}"
    return await cloud_generate(
        "conversation",
        system,
        recent + [{"role": "user", "content": message}],
        config,
    )


def _facts_context(config: FeltConfig) -> list[dict]:
    facts = get_all_facts(config.db_path)
    if not facts:
        return []
    bullet_list = "\n".join(f"- {fact}" for fact in facts)
    return [{"role": "system", "content": f"Known facts about the user:\n{bullet_list}"}]


def _music_taste_context(config: FeltConfig) -> list[dict]:
    """WP-16 Part 2 - unlike _facts_context, this is fine on the cloud
    path too (Mario, 2026-09-22: music is explicitly cloud-ok, same as
    email/calendar), so both _local_reply and _cloud_chat include it
    unconditionally. Empty until enough listening history exists to say
    anything (see music_taste.summarize_taste)."""
    note = summarize_taste(get_music_taste_stats(config.db_path))
    if not note:
        return []
    return [{"role": "system", "content": note}]


async def _local_reply(
    messages_tail: list[dict], config: FeltConfig, *, include_facts: bool, tone: str | None = None
) -> str:
    """The local (Ollama) reply path, factored out of three near-identical
    call sites (2026-09-22, WP-02) - plain chat's fallback, and both of
    screen-context's local branches. Always carries the personality
    prompt; facts (Mario's remembered personal info) are opt-in per
    caller, since screen-reaction replies don't need them (WP-02: "Facts
    are not needed; don't include them")."""
    # include_facts also gates music taste: both are "personal context a
    # plain-chat reply wants but a screen-reaction body-language line
    # doesn't" (WP-02's original reasoning for facts applies just the
    # same here), even though music taste - unlike facts - is allowed on
    # the cloud path too (see _music_taste_context).
    facts = _facts_context(config) if include_facts else []
    taste = _music_taste_context(config) if include_facts else []
    messages = [{"role": "system", "content": _personality_prompt(tone)}] + facts + taste + messages_tail
    return await _ollama_chat(messages, config.ollama_model, config, max_tokens=_REPLY_MAX_TOKENS)


async def _chat_reply(
    message: str, history: list[dict], config: FeltConfig, safe: bool | None, tone: str | None = None
) -> str:
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
                reply = await _cloud_chat(message, history, config, tone)
            logger.info("chat reply: routed to cloud (%s)", config.cloud_model_conversation)
            return reply
        except Exception:
            logger.exception("cloud chat call failed, falling back to local")
    logger.info("chat reply: local (%s)", config.ollama_model)
    async with metrics.timed("reply", route="local", model=config.ollama_model):
        return await _local_reply(
            history + [{"role": "user", "content": message}], config, include_facts=True, tone=tone
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


# NOT a tight cap - counterintuitively has to be generous. Real bug found
# live, 2026-09-22 (WP-06's own first weather-precompute run): the volume
# model (gemma-4-26b-a4b-it) burns hidden "thinking" tokens before its
# real answer (each flagged `"thought": true` - see cloud.py's
# _extract_text), and the WP's original 120-token cap was cutting it off
# entirely mid-thought - the "reply" was Gemma's raw internal reasoning
# transcript, not a spoken line at all. Measured against the ACTUAL
# production prompt shape (personality + the full tool-result
# instruction), not a toy one: 473 thinking tokens + a real 37-token
# answer = 510 total, comfortably over an initial 500 retry that still
# truncated it. 1000 leaves real headroom above that measured number.
# The final answer itself is still short (~20-40 tokens, same "1-2
# sentences" instruction as everywhere else) - this budget only has to
# cover the thinking overhead ahead of it, not make replies any longer,
# and Gemma's free-tier TPM budget (16K/min) makes the extra headroom
# essentially free.
# 2026-09-24 (audit): 1000 still wasn't enough - 71 of 84 weather
# precomputes came back all-thinking (997 thought tokens, MAX_TOKENS).
# The real fix is cloud.py asking for thinkingLevel "minimal" on
# skill_phrasing (~22 output tokens, no truncation); this budget stays as
# headroom for a model that rejects thinkingConfig.
_SKILL_PHRASING_MAX_TOKENS = 1000


async def _phrase_skill_result(
    message: str,
    tool_output: str,
    history: list[dict],
    skill_privacy: str,
    config: FeltConfig,
    tone: str | None = None,
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
                cloud_reply = await cloud_generate(
                    "skill_phrasing", _personality_prompt(tone), messages, config, max_tokens=_SKILL_PHRASING_MAX_TOKENS
                )
            # An empty string isn't an exception - cloud_generate returns
            # it deliberately (cloud.py's _extract_text) when a model's
            # response was nothing but hidden "thinking" content with no
            # real answer. Treated as a failure here too, same as any
            # other cloud problem: better a working local reply than a
            # silent non-answer.
            if cloud_reply:
                return cloud_reply
            logger.warning("cloud skill phrasing returned nothing usable, falling back to local")
        except Exception:
            logger.exception("cloud skill phrasing failed, falling back to local")

    # WP-02: the shared local helper (personality + facts + music taste +
    # the length cap), not a hand-built copy of it.
    async with metrics.timed("reply", route="local", model=config.ollama_model):
        return await _local_reply(
            history
            + [
                {"role": "user", "content": message},
                {"role": "user", "content": _tool_result_instruction(tool_output)},
            ],
            config,
            include_facts=True,
            tone=tone,
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
            cloud_line = await cloud_generate(
                "skill_phrasing",
                _PERSONALITY_PROMPT,
                [
                    {"role": "user", "content": "what's the weather right now?"},
                    {"role": "user", "content": _tool_result_instruction(tool_output)},
                ],
                config,
                max_tokens=_SKILL_PHRASING_MAX_TOKENS,
            )
            # Same "empty isn't success" rule as _phrase_skill_result -
            # cloud_generate can return "" deliberately (all-thinking, no
            # real answer) without raising.
            if cloud_line:
                line = cloud_line
            else:
                logger.warning("weather precompute: cloud phrasing returned nothing usable, using the raw tool output")
        except Exception:
            logger.exception("weather precompute: cloud phrasing failed, using the raw tool output")

    tts_text = strip_for_tts(line)
    # Same line as last time (the weather didn't change) - already cached,
    # no need to re-render it.
    if config.voice_url and tts_cache.get(tts_text) is None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                tts_resp = await client.post(f"{config.voice_url}/v1/tts", json={"text": tts_text})
                tts_resp.raise_for_status()
            tts_cache.put(tts_text, tts_resp.content)
        except httpx.HTTPError as e:
            # The 2026-09-24 audit's ~76 failures/24h were all
            # httpcore.ConnectError: felt-voice on the Nitro5 was down
            # (09-23 22:30 -> 09-24 17:45), not a felt-core bug. One line,
            # not a full traceback every 15 minutes; a live request just
            # renders TTS itself on a cache miss.
            logger.warning("weather precompute: TTS pre-render skipped, felt-voice unavailable (%r)", e)
        except Exception:
            logger.exception("weather precompute: TTS pre-render failed")

    _weather_precompute = {
        "line": line,
        "animation_tag": result.animation_tag,
        "computed_at": time.monotonic(),
    }
    logger.info("weather precompute refreshed: %r", line)


def current_weather() -> dict:
    """WP-21: the precompute's current condition for the desktop (props:
    umbrella when it rains, sunglasses when it's sunny) - "rain" / "sun" /
    "snow" / "storm" from the get_weather condition tag, None when the
    code has no mapping or nothing fresh is cached. No text, just the
    condition."""
    fresh = _weather_precompute_fresh()
    if fresh is None:
        return {"condition": None, "animation_tag": None, "age_s": None}
    tag = fresh.get("animation_tag")
    condition = tag.rsplit("/", 1)[-1] if isinstance(tag, str) and tag.startswith("reaction/weather/") else None
    return {
        "condition": condition,
        "animation_tag": tag,
        "age_s": round(time.monotonic() - fresh["computed_at"]),
    }


async def weather_precompute_loop(config: FeltConfig) -> None:
    while True:
        try:
            await refresh_weather_precompute(config)
        except Exception:
            logger.exception("weather precompute loop iteration failed")
        await asyncio.sleep(_WEATHER_PRECOMPUTE_INTERVAL_S)


_MUSIC_TASTE_POLL_INTERVAL_S = 15.0


def _track_key(track: dict | None) -> str | None:
    """A stable identity for a Spotify item. Local files (and some other
    non-catalogue items) come back with `id: null` but still carry a
    `uri` ("spotify:local:..."), so that's the fallback key. None means
    the item can't be keyed at all and is simply not tracked - the
    music_taste table's track_id is NOT NULL, and a null id there wedged
    music_taste_loop on one item every 15 s (audit 2026-09-24)."""
    if not track:
        return None
    return track.get("id") or track.get("uri") or None


def _finished_play_from_transition(previous: dict | None, current: dict | None) -> dict | None:
    """Pure: `previous`/`current` are get_currently_playing snapshots (or
    None) from two consecutive polls. A listen "finishes" - and gets
    returned as a record ready for db.record_music_play(**record) - when
    the track id changed or playback stopped since `previous`; returns
    None when the same track is still playing, or there was nothing
    playing before to finish. `played_ms` is `previous`'s last-seen
    progress, not a precise stop time - it can undercount actual listen
    time by up to one poll interval, an accepted approximation."""
    if previous is None:
        return None
    prev_track = previous["track"]
    prev_key = _track_key(prev_track)
    if prev_key is None:
        # Nothing to key the listen on (no id, no uri) - not recordable.
        return None
    current_track = current["track"] if current else None
    if current_track is not None and _track_key(current_track) == prev_key:
        return None
    return {
        "track_id": prev_key,
        "track_name": prev_track.get("name", ""),
        "artists": ", ".join(a["name"] for a in prev_track.get("artists", [])),
        "played_ms": previous["progress_ms"],
        "duration_ms": previous["duration_ms"],
        "skipped": is_skip(previous["progress_ms"], previous["duration_ms"]),
    }


async def music_taste_loop(config: FeltConfig) -> None:
    """WP-16 Part 2: polls Spotify's currently-playing endpoint to build a
    skip/full-play history per track (see music_taste.summarize_taste for
    how it's surfaced back into the prompt). Runs independently of
    felt-tray's own /v1/music/now polling (that one drives unprompted
    "oh, this one again" remarks, not taste tracking) - both happen to
    poll at a similar cadence for different reasons, and neither depends
    on the other."""
    previous: dict | None = None
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            try:
                if config.spotify_refresh_token:
                    current = await get_currently_playing(client, config)
                    finished = _finished_play_from_transition(previous, current)
                    # Advance BEFORE recording: a record that fails must
                    # never leave `previous` pointing at the same item, or
                    # every later poll retries (and fails on) it forever.
                    previous = current
                    if finished:
                        try:
                            record_music_play(config.db_path, **finished)
                        except Exception:
                            logger.exception("music taste: couldn't record %r, skipping it", finished.get("track_name"))
            except Exception:
                logger.exception("music taste loop iteration failed")
            await asyncio.sleep(_MUSIC_TASTE_POLL_INTERVAL_S)


async def chat(
    message: str, config: FeltConfig, session_id: str = "default", tone: str | None = None
) -> ChatResult:
    # Checked before anything else, including history/facts loading — a
    # bare "yes" answering the proactive "want me to summarize it?" ping
    # (email_watcher.py) has to be caught here or it falls through to the
    # router with zero context and either misroutes or gets a generic
    # chat reply that doesn't know any email exists.
    if _looks_affirmative(message) and get_unresolved_pending_email(config.db_path):
        summary = await _summarize_pending_email(config, session_id, message)
        return ChatResult(reply=summary)

    registry = load_plugins(config.plugins_dir)
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
        # WP-09: a "shut up"/"you can talk again" match skips the router
        # LLM call entirely, not just its own decision - the whole point of
        # this fast path is Mario never has to wait out a model round trip
        # (or risk a misroute) to get her to stop talking.
        fast_call = _presence_fast_path(message)
        call = fast_call or await decide_tool(message, registry, config)

    if (
        fast_call is None
        and call is not None
        and call[0] == "felt_presence"
        and not _router_presence_allowed(message, call[1])
    ):
        logger.info(
            "presence guard: dropping router's felt_presence %r - no explicit request in message",
            call[1].get("intent"),
        )
        call = None

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
        reply = await _chat_reply(message, history, config, safe, tone)
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
    reply = await _phrase_skill_result(message, tool_output, history, skill_privacy, config, tone)
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


async def process_screen_context(
    description: str, config: FeltConfig, session_id: str = "default", tone: str | None = None
) -> ChatResult:
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
        # "Real" excludes her own earlier screen remarks (audit
        # 2026-09-24) - those are saved as assistant messages too.
        history_tail = get_history(config.db_path, session_id, exclude_remarks=True)[-4:]
        async with metrics.timed("screen_reaction", route="local", model=config.ollama_model):
            return await _local_reply(
                [{"role": "system", "content": observations_block}]
                + history_tail
                + [{"role": "user", "content": _SCREEN_REACTION_PROMPT}],
                config,
                include_facts=False,
                tone=tone,
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
            async with metrics.timed("screen_reaction", route="cloud", model=config.cloud_model_volume):
                reply_raw = await cloud_generate(
                    "screen_reaction",
                    f"{_personality_prompt(tone)}\n\n{observations_block}",
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


# WP-07's fixed emotion set - anything else that comes back gets folded to
# "neutral" rather than passed through as an arbitrary string (X-Emotion
# is meant to drive a small fixed set of expressions client-side, not an
# open vocabulary).
_EMOTION_VALUES = frozenset(
    {"happy", "excited", "amused", "sad", "annoyed", "surprised", "thinking", "sleepy", "neutral"}
)

_SCREEN_REACT_INSTRUCTION = (
    "Look at the screenshot. Respond with ONLY a JSON object, no other text, "
    "no markdown code fences, with exactly these three fields:\n"
    '{"seen": "<one short neutral sentence describing what is actually on '
    'screen>", "line": "<your spoken reaction, max ~15 words, in character, '
    "never mention screens or screenshots>\", "
    '"emotion": "<one of: happy, excited, amused, sad, annoyed, surprised, '
    'thinking, sleepy, neutral>"}'
)


def _parse_screen_react_json(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


async def react_to_screen(
    jpeg_bytes: bytes,
    app: str,
    title: str,
    config: FeltConfig,
    session_id: str = "default",
    trigger: str = "focus",
    tone: str | None = None,
) -> ChatResult:
    """WP-07's new per-window, image-based screen reaction path. The
    ONLY model call is a single multimodal cloud_generate("screen_reaction",
    ..., images=[jpeg_bytes], json_mode=True) - images never reach the
    local Ollama here, since there's no local VLM on the server. Unlike
    process_screen_context there is deliberately no local fallback: a
    cloud failure (CloudUnavailable) propagates straight up to the caller
    (api.py maps it to a 503 cloud_unavailable), and felt-tray's watcher
    falls back to the OLD felt-vision + process_screen_context() path
    itself when it sees that.

    `trigger="manual"` (WP-10's future "look at this" key) bypasses the
    cooldown - every other trigger respects it, checked BEFORE any model
    call, same as process_screen_context."""
    if trigger != "manual":
        seconds_since = seconds_since_last_spoken_reaction(config.db_path, session_id)
        if seconds_since is not None and seconds_since < config.screen_reaction_cooldown_s:
            logger.info(
                "screen reaction: on cooldown (%.0fs since last spoken reaction, need %ds) - no model call",
                seconds_since,
                config.screen_reaction_cooldown_s,
            )
            return ChatResult(reply="", animation_tag=None)

    observations_block = _observations_block(get_recent_observations(config.db_path, session_id, limit=3))
    system = f"{_personality_prompt(tone)}\n\n{observations_block}"
    context_line = f'App: {app} — "{title}"'

    # WP-04's screen_reaction stage (the model call(s), cloud route) -
    # WP-05 had left only the generic `cloud` stage here (audit 2026-09-24).
    async with metrics.timed("screen_reaction", route="cloud", model=config.cloud_model_volume):
        raw = await cloud_generate(
            "screen_reaction",
            system,
            [{"role": "user", "content": f"{context_line}\n\n{_SCREEN_REACT_INSTRUCTION}"}],
            config,
            images=[jpeg_bytes],
            json_mode=True,
        )
        parsed = _parse_screen_react_json(raw)

        if parsed is None:
            logger.warning("screen reaction: JSON parse failed on the first attempt, retrying once")
            retry_raw = await cloud_generate(
                "screen_reaction",
                system,
                [
                    {
                        "role": "user",
                        "content": f"{context_line}\n\n{_SCREEN_REACT_INSTRUCTION}\n\n"
                        "Respond with ONLY that raw JSON object - no markdown, no code fences, no other text.",
                    }
                ],
                config,
                images=[jpeg_bytes],
            )
            parsed = _parse_screen_react_json(retry_raw)

    if parsed is None:
        logger.warning("screen reaction: JSON parse failed twice - giving up, no reaction")
        return ChatResult(reply="", animation_tag=None)

    seen = str(parsed.get("seen") or "").strip()
    line = str(parsed.get("line") or "").strip()
    emotion = parsed.get("emotion")
    emotion = emotion if emotion in _EMOTION_VALUES else "neutral"

    obs_id = save_observation(config.db_path, session_id, seen or f"(on screen: {app})")

    if not line:
        logger.warning("screen reaction: JSON parsed but 'line' was empty - no reaction")
        return ChatResult(reply="", animation_tag=None)

    set_observation_reply(config.db_path, obs_id, line)
    save_message(config.db_path, session_id, "assistant", line)
    return ChatResult(reply=line, animation_tag=None, emotion=emotion)
