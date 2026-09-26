"""The brain loop: a small router decides tool-vs-chat, the main model
only ever converses. Real conversation memory (2026-09-11) - a session's
prior messages are loaded from SQLite and included, so a follow-up
question actually has context.

2026-09-11 redesign (kept from before): routing is a separate, narrow
classification call to a small dedicated model (config.router_model); the
main model (config.ollama_model, chosen for chat quality) never sees
tool-call instructions at all, it just answers.

2026-09-21: plain conversation (no skill involved) can also be routed to
Gemini for a better reply, gated by a second small-model classification
(privacy.py) - see replies._chat_reply. Only the skill-free path is
eligible; a tool-result reply always stays local, since tool output can
carry real personal data (email/calendar contents, etc.).

WP-40: split out of the single 1080-line brain.py into this package.
chat() itself - routing/privacy dispatch and skill execution - lives
here; reply generation (local/cloud) is replies.py, screen reactions are
screen_reactions.py, and the weather/music-taste background loops are
background.py. felt_core.brain re-exports the public entry points so
`from felt_core.brain import chat, ...` (api.py, __init__.py) is
unchanged.
"""

import asyncio
import logging
import re
from collections.abc import Callable

from felt_core import metrics
from felt_core.animation import resolve_animation_tag
from felt_core.brain.background import _WEATHER_NOW_PHRASES, _weather_precompute_fresh
from felt_core.brain.personality import PERSONALITY_PROMPT
from felt_core.brain.replies import (
    _chat_reply,
    _format_for_privacy_check,
    _phrase_skill_result,
    _timed_privacy_check,
    local_reply,
)
from felt_core.brain.result import ChatResult
from felt_core.cloud import cloud_generate
from felt_core.config import FeltConfig
from felt_core.db import (
    get_current_screen_description,
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

logger = logging.getLogger(__name__)

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
    "unhush",
    "ok_talk",
    "you_can_talk_again",
    "come_back",
    "come",
    "come_here",
    "move",
    "move_over",
    "shoo",
    "look",
    "look_at_this",
    "repeat",
    "what_did_you_say",
    "say_that_again",
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
                summary = await cloud_generate(
                    "summary", PERSONALITY_PROMPT, [{"role": "user", "content": prompt}], config
                )
        except Exception:
            logger.exception("cloud email summary failed, falling back to local")
    if summary is None:
        # Same shape as every other local reply (personality prompt + the
        # spoken-length cap) - it used to be a bare, uncapped prompt (audit
        # 2026-09-24). No facts: the email body is all the context needed.
        async with metrics.timed("reply", route="local", model=config.ollama_model):
            summary = await local_reply(
                [{"role": "user", "content": prompt}], config, include_facts=False
            )

    mark_email_resolved(config.db_path, pending["id"])
    save_message(config.db_path, session_id, "user", message)
    save_message(config.db_path, session_id, "assistant", summary)
    return summary


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
        asyncio.create_task(
            _timed_privacy_check(_format_for_privacy_check(history, message), config)
        )
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
                logger.info(
                    "weather fast path: serving the precomputed line, no skill/phrasing calls"
                )
                save_message(config.db_path, session_id, "assistant", precompute["line"])
                return ChatResult(
                    reply=precompute["line"], animation_tag=precompute["animation_tag"]
                )

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
