"""One step of a tool-calling turn.

Run a skill call, phrase its result, force a tool turn; the weather fast line.
"""

import logging
from collections.abc import Callable

import httpx2

from felt_core.brain import router
from felt_core.brain.background import _WEATHER_NOW_PHRASES, _weather_precompute_fresh
from felt_core.brain.replies import (
    _REPLY_MAX_TOKENS,
)
from felt_core.brain.result import ChatResult
from felt_core.brain.streaming import ReplySpeaker
from felt_core.clients import local_llm
from felt_core.clients.spotify_client import music_playing_cached
from felt_core.config import FeltConfig
from felt_core.core import metrics
from felt_core.core.animation import resolve_animation_tag
from felt_core.core.plugin_loader import LoadedSkill
from felt_core.core.tools import run_skill
from felt_core.db.messages import save_message
from felt_core.dj import control as dj_control
from felt_core.models.plugin import ExecutionContext

logger = logging.getLogger(__name__)


# Skills that only read. While every call so far was one of these, an "I've
# got it set" in her follow-up is a fake claim and gets a tool forced; once
# something acted, "I'll nudge you at ten" is true, and forcing a tool could
# set the reminder twice. A skill not listed counts as acting (never forced).
_LOOKUP_SKILLS = frozenset(
    {
        "board_status",
        "check_email",
        "diary_recall",
        "get_container_status",
        "get_gpu_status",
        "get_phone_battery",
        "get_recent_alerts",
        "get_time",
        "get_weather",
        "list_alarms",
        "list_calendar_events",
        "news_digest",
        "now_playing",
        "poe2_wiki",
        "poe_price_check",
        "printer_status",
        "search_documents",
        "web_search",
    }
)


def _weather_fast_line(call: tuple[str, dict] | None) -> dict | None:
    """WP-06 fast path: the precomputed "weather right now" line.

    if the picked call is that question and a fresh one exists.
    """
    if call is None or call[0] != "get_weather":
        return None
    day = str(call[1].get("day", "now") or "now").lower()
    if day not in _WEATHER_NOW_PHRASES:
        return None
    return _weather_precompute_fresh()


async def _run_skill_call(call: tuple[str, dict], registry: dict[str, LoadedSkill], config: FeltConfig, message: str):
    """Runs the picked skill; returns (SkillResult | None, tool_output).

    None (not the ValueError branch's job alone) so the animation/ui_action
    lookup has something safe to check regardless of which path was taken -
    a skill that raised never gets a reaction.
    """
    skill_name, params = call
    result = None
    logger.info("skill call: %s", skill_name)
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
    except httpx2.HTTPError as e:
        # the service behind the skill is down or erroring (poe2-rag, drive
        # search, weather, searxng...): was an unhandled crash - the whole
        # chat 500'd (e2e 2026-10-18). She says so instead of guessing.
        logger.warning("skill %s: service unavailable (%r)", skill_name, e)
        tool_output = (
            f"Error: couldn't reach the service behind {skill_name} right now. "
            "Say so plainly - don't answer from memory as if you'd looked it up."
        )
    return result, tool_output


def _skill_chat_result(reply: str, result, skill_name: str, registry: dict[str, LoadedSkill]) -> ChatResult:
    if result is None:
        return ChatResult(reply=reply)
    return ChatResult(
        reply=reply,
        ui_action=result.ui_action,
        animation_tag=resolve_animation_tag(registry.get(skill_name), result),
    )


async def _reply_turn(
    messages: list[dict],
    config: FeltConfig,
    speaker: ReplySpeaker | None,
    *,
    tools: list[dict] | None = None,
    guard: Callable[[str], bool] | None = None,
) -> local_llm.LocalReply:
    """A felt-llm call that may be her answer.

    streamed through the speaker when chat_streamed() is running the turn (WP-65), else
    the plain call. `guard` spots a fake claim in the streamed opening.
    """
    async with metrics.timed("reply", route="local", model=config.local_llm_model):
        if speaker is None:
            return await local_llm.chat(messages, max_tokens=_REPLY_MAX_TOKENS, tools=tools, config=config)
        return await speaker.turn(messages, max_tokens=_REPLY_MAX_TOKENS, tools=tools, guard=guard, config=config)


async def _forced_tool_turn(messages: list[dict], tools: list[dict], config: FeltConfig) -> local_llm.LocalReply | None:
    """FELT-108: the same call again with a tool call required, after a reply claimed a lookup or action it never did.

    None when that fails or still calls nothing - the caller keeps the plain reply (never worse than before).
    """
    try:
        async with metrics.timed("reply", route="local", model=config.local_llm_model):
            forced = await local_llm.chat(
                messages,
                max_tokens=_REPLY_MAX_TOKENS,
                tools=tools,
                tool_choice="required",
                config=config,
            )
    except local_llm.LocalLLMUnavailableError as e:
        logger.warning("forced retry failed (%s) - keeping the plain reply", e)
        return None
    if not forced.tool_calls:
        logger.warning("forced retry still called no tool - keeping the plain reply")
        return None
    return forced


async def _said(config: FeltConfig, session_id: str, reply: str, speaker: ReplySpeaker | None) -> str:
    """The reply as she said it (with anything already let out in front, WP-65), written to the history.

    once per turn, at the end.
    """
    if speaker is not None:
        reply = speaker.full_reply(reply)
    await save_message(config.db_path, session_id, "assistant", reply)
    return reply


def _router_context(history: list[dict]) -> router.Context:
    previous = next((m.get("content") for m in reversed(history) if m.get("role") == "user"), None)
    return router.Context(
        previous=previous if isinstance(previous, str) else None,
        music_playing=music_playing_cached(),
        dj_on=dj_control.is_on(),
    )
