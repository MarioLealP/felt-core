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

from felt_core import local_llm, metrics
from felt_core.animation import resolve_animation_tag
from felt_core.brain.background import _WEATHER_NOW_PHRASES, _weather_precompute_fresh
from felt_core.brain.personality import PERSONALITY_PROMPT
from felt_core.brain.replies import (
    _REPLY_MAX_TOKENS,
    _chat_reply,
    _format_for_privacy_check,
    _phrase_skill_result,
    _timed_privacy_check,
    _tool_result_instruction,
    legacy_config,
    local_model_label,
    local_reply,
    local_turn_messages,
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
from felt_core.plugin_loader import LoadedSkill, load_plugins
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


def _apply_call_guards(
    call: tuple[str, dict] | None, fast_call: tuple[str, dict] | None, message: str
) -> tuple[str, dict] | None:
    """The presence guard and the per-skill guards, shared by the router
    path and the WP-36 local tool-calling path: a model's pick only stands
    if it clears them. `fast_call` (the deterministic presence regex) is
    never second-guessed."""
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
        return None

    if call is not None:
        guard = _SKILL_GUARDS.get(call[0])
        if guard is not None and not guard(message):
            logger.info(
                "skill guard: overriding router's '%s' pick - no matching intent in message",
                call[0],
            )
            return None
    return call


def _weather_fast_line(call: tuple[str, dict] | None) -> dict | None:
    """WP-06 fast path: the precomputed "weather right now" line, if the
    picked call is that question and a fresh one exists."""
    if call is None or call[0] != "get_weather":
        return None
    day = str(call[1].get("day", "now") or "now").lower()
    if day not in _WEATHER_NOW_PHRASES:
        return None
    return _weather_precompute_fresh()


async def _run_skill_call(
    call: tuple[str, dict], registry: dict[str, LoadedSkill], config: FeltConfig, message: str
):
    """Runs the picked skill; returns (SkillResult | None, tool_output).
    None (not the ValueError branch's job alone) so the animation/ui_action
    lookup has something safe to check regardless of which path was taken -
    a skill that raised never gets a reaction."""
    skill_name, params = call
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
    return result, tool_output


def _skill_chat_result(
    reply: str, result, skill_name: str, registry: dict[str, LoadedSkill]
) -> ChatResult:
    if result is None:
        return ChatResult(reply=reply)
    return ChatResult(
        reply=reply,
        ui_action=result.ui_action,
        animation_tag=resolve_animation_tag(registry.get(skill_name), result),
    )


def _skill_tools(registry: dict[str, LoadedSkill]) -> list[dict]:
    """The registry as OpenAI-style tool definitions for felt-llm."""
    return [
        {
            "type": "function",
            "function": {
                "name": skill.name,
                "description": skill.description,
                "parameters": skill.parameters_schema,
            },
        }
        for skill in registry.values()
    ]


async def _chat_local(
    message: str,
    config: FeltConfig,
    session_id: str,
    tone: str | None,
    registry: dict[str, LoadedSkill],
    history: list[dict],
) -> ChatResult:
    """WP-36 local-first turn: ONE felt-llm call carries the persona, the
    history, the message and every skill as a native tool. A plain reply
    comes back as text; a tool call goes through the same guards, weather
    fast path and run_skill as the router path, then a SECOND call on the
    same prefix (+ the tool_call and its result) phrases the answer.
    Nothing here goes near the privacy classifier - it never leaves the
    LAN. Raises LocalLLMUnavailable only before any skill has run (the
    caller then falls back to the whole old path); once a skill has run,
    a failed phrasing call degrades to _phrase_skill_result instead, so
    the skill is never executed twice."""
    prefix = local_turn_messages(message, history, config, tone)
    tools = _skill_tools(registry)

    # The presence fast path skips the model entirely (WP-09): "shut up"
    # must never wait on, or be second-guessed by, a model round trip.
    fast_call = _presence_fast_path(message)
    call = fast_call
    first: local_llm.LocalReply | None = None
    tool_call: dict | None = None
    if fast_call is None:
        async with metrics.timed("reply", route="local", model=config.local_llm_model):
            first = await local_llm.chat(
                prefix, max_tokens=_REPLY_MAX_TOKENS, tools=tools, config=config
            )
        if first.tool_calls:
            tool_call = first.tool_calls[0]
            name, params = local_llm.parse_tool_arguments(tool_call)
            call = (name, params) if name else None

    call = _apply_call_guards(call, fast_call, message)

    precompute = _weather_fast_line(call)
    if precompute is not None:
        logger.info("weather fast path: serving the precomputed line, no skill/phrasing calls")
        save_message(config.db_path, session_id, "assistant", precompute["line"])
        return ChatResult(reply=precompute["line"], animation_tag=precompute["animation_tag"])

    if call is None:
        # No tool (or a guard dropped the model's pick): the text is the
        # reply. A dropped pick usually has no text, so ask once more
        # without tools.
        reply = first.text.strip() if first is not None else ""
        if not reply:
            async with metrics.timed("reply", route="local", model=config.local_llm_model):
                plain = await local_llm.chat(prefix, max_tokens=_REPLY_MAX_TOKENS, config=config)
            reply = plain.text.strip()
        save_message(config.db_path, session_id, "assistant", reply)
        return ChatResult(reply=reply)

    skill_name = call[0]
    result, tool_output = await _run_skill_call(call, registry, config, message)
    skill_privacy = registry[skill_name].privacy if skill_name in registry else "local"

    reply = ""
    if tool_call is not None and first is not None:
        followup = prefix + [
            {"role": "assistant", "content": first.text, "tool_calls": [tool_call]},
            {
                "role": "tool",
                "tool_call_id": str(tool_call.get("id") or ""),
                "content": _tool_result_instruction(tool_output),
            },
        ]
        try:
            async with metrics.timed("reply", route="local", model=config.local_llm_model):
                second = await local_llm.chat(
                    followup, max_tokens=_REPLY_MAX_TOKENS, tools=tools, config=config
                )
            reply = second.text.strip()
        except local_llm.LocalLLMUnavailable:
            logger.warning("tool-result phrasing: felt-llm unavailable, using the old path")
            config = legacy_config(config)
    if not reply:
        # The presence fast path (no model turn yet), or an empty/failed
        # phrasing call: the shared phrasing step, which is itself
        # local-first with the cloud/Ollama fallback.
        reply = await _phrase_skill_result(
            message, tool_output, history, skill_privacy, config, tone
        )
    save_message(config.db_path, session_id, "assistant", reply)
    return _skill_chat_result(reply, result, skill_name, registry)


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
    if config.local_first:
        # WP-36: the email body never needs to leave the LAN when felt-llm
        # is up; the cloud summary below is only the fallback.
        try:
            async with metrics.timed("reply", route="local", model=config.local_llm_model):
                summary = (
                    await local_reply(
                        [{"role": "user", "content": prompt}], config, include_facts=False
                    )
                ) or None
        except local_llm.LocalLLMUnavailable:
            logger.warning("email summary: felt-llm unavailable, using the old path")
            config = legacy_config(config)
    if summary is None and config.gemini_api_key:
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
        async with metrics.timed("reply", route="local", model=local_model_label(config)):
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

    if config.local_first:
        try:
            return await _chat_local(message, config, session_id, tone, registry, history)
        except local_llm.LocalLLMUnavailable:
            logger.warning("chat: felt-llm unavailable, falling back to the router/cloud path")
            config = legacy_config(config)

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

    call = _apply_call_guards(call, fast_call, message)

    # WP-06 fast path: the router already did the real classification work
    # (deciding this IS a "weather right now" question) - if a fresh
    # precomputed answer exists, skip run_skill AND the phrasing model
    # entirely and hand back that exact line, so api._synthesize_reply's
    # TTS cache (keyed on that same text, warmed by the precompute) is a
    # guaranteed hit too.
    precompute = _weather_fast_line(call)
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

    skill_name = call[0]
    result, tool_output = await _run_skill_call(call, registry, config, message)

    skill_privacy = registry[skill_name].privacy if skill_name in registry else "local"
    reply = await _phrase_skill_result(message, tool_output, history, skill_privacy, config, tone)
    save_message(config.db_path, session_id, "assistant", reply)

    return _skill_chat_result(reply, result, skill_name, registry)
