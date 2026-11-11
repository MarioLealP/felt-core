"""The brain loop: one felt-llm call carries the persona, the history, the message and every skill as a native tool.

(WP-36 D'': felt-llm is the only backend - no router model, no cloud, no fallback). Real conversation
memory (2026-09-11) - a session's prior messages are loaded from SQLite
and included, so a follow-up question actually has context.

WP-40: split out of the single 1080-line brain.py into this package.
chat() itself - the local tool-calling turn and skill execution - lives
here; reply generation is replies.py, screen reactions are
screen_reactions.py, and the weather/music-taste/warm-up background loops
are background.py. felt_core.brain re-exports the public entry points so
`from felt_core.brain import chat, ...` (api.py, __init__.py) is
unchanged.

When felt-llm is unreachable, `local_llm.LocalLLMUnavailableError` propagates
out of chat() and the API answers 503.
"""

import asyncio
import logging
from collections.abc import AsyncIterator

from felt_core.brain import router
from felt_core.brain.call_guards import _apply_call_guards, _looks_affirmative, _presence_fast_path
from felt_core.brain.lookup_claims import claims_a_tool_use, claims_an_action
from felt_core.brain.music_fast_path import music_fast_path
from felt_core.brain.replies import (
    _tool_result_instruction,
    local_reply,
    local_turn_messages,
    phrase_skill_result,
    skill_tools,
)
from felt_core.brain.result import ChatResult
from felt_core.brain.skill_turns import (
    _LOOKUP_SKILLS,
    _forced_tool_turn,
    _reply_turn,
    _router_context,
    _run_skill_call,
    _said,
    _skill_chat_result,
    _weather_fast_line,
)
from felt_core.brain.streaming import ReplyMeta, ReplySpeaker, correction_note
from felt_core.clients import ha_assist, local_llm
from felt_core.config import FeltConfig
from felt_core.core import emails, features, metrics
from felt_core.core.plugin_loader import LoadedSkill, load_plugins
from felt_core.db.messages import get_her_recent_lines, get_history, save_message
from felt_core.db.observations import get_current_screen_description

logger = logging.getLogger(__name__)


# cm-p6a (2026-10-22): "google the launch time and set me a reminder an hour
# before" needs two tools - with one tool per message she searched, then
# said "I've got it set for you" with nothing set. After each tool result
# she may call another; capped (a stuck model repeating a search would hold
# felt-llm for minutes) and a repeated call ends the chain.
_MAX_TOOL_STEPS = 5
_CHAIN_NOTE = (
    "If what he asked still needs another tool to be done (he asked for more than one "
    "thing, e.g. to find something and then set a reminder), call that tool now instead "
    "of answering. Never say something is done unless its tool result is above."
)


async def _chat_local(
    message: str,
    config: FeltConfig,
    session_id: str,
    tone: str | None,
    registry: dict[str, LoadedSkill],
    history: list[dict],
    speaker: ReplySpeaker | None = None,
) -> ChatResult:
    """One felt-llm call carries the persona, the history, the message and every skill as a native tool.

    A plain reply comes back as text; a tool call goes through the guards, the weather
    fast path and run_skill, then a SECOND call on the same prefix (+ the tool_call and
    its result) phrases the answer. Raises LocalLLMUnavailableError if felt-llm is down at
    any point (the API answers 503).
    """
    prefix = local_turn_messages(message, history, config, tone)
    tools = skill_tools(registry)
    # cm-6b0: the semantic router's pick (a few ms, in a thread while
    # ha_assist runs), recorded next to what she does
    pick_task = router.start_pick(message, _router_context(history))

    # The presence fast path skips the model entirely (WP-09): "shut up"
    # must never wait on, or be second-guessed by, a model round trip.
    # WP-48: so does a plain music command ("skip").
    fast_call = _presence_fast_path(message) or music_fast_path(message)
    if fast_call is None:
        # a plain home or list command: Home Assistant's own matcher does it
        # (ha_assist.py; replaced the home_fast_path regex, 2026-10-19)
        done = await ha_assist.handle(config, message)
        if done is not None:
            router.record_pick(config, pick_task, message, router.HA_ASSIST, fast=True)
            async with metrics.timed("reply", route="local", model=config.local_llm_model):
                reply = await phrase_skill_result(
                    message, f"Home Assistant did it. It says: {done}", history, config, tone
                )
            return ChatResult(reply=_said(config, session_id, reply, speaker))
    call = fast_call
    first: local_llm.LocalReply | None = None
    tool_call: dict | None = None
    claimed = False
    if fast_call is None:
        sure = await router.sure_skill(config, pick_task)
        if sure is not None and sure.skill in registry:
            # cm-6b0 act mode: it's clearly a request for a skill, so her
            # first turn must call a tool - never a plain "sure, added!"
            logger.info(
                "router: sure of %s (%.2f, %s) - tool call required",
                sure.skill,
                sure.score,
                sure.via,
            )
            first = await _forced_tool_turn(prefix, tools, config)
        if first is None:
            first = await _reply_turn(prefix, config, speaker, tools=tools, guard=claims_a_tool_use)
        if not first.tool_calls and claims_a_tool_use(first.text):
            claimed = True
            # FELT-108: she said she'd look it up ("pulling up the wiki...")
            # or do it ("turning the shelf light off") but called nothing.
            # Ask again with a tool call required, so the claim is true or
            # the answer honest.
            logger.warning("reply claimed a lookup or action without a tool call - retrying with a tool required")
            forced = await _forced_tool_turn(prefix, tools, config)
            if forced is not None:
                first = forced
        if first.tool_calls:
            tool_call = first.tool_calls[0]
            name, params = local_llm.parse_tool_arguments(tool_call)
            call = (name, params) if name else None

    call = _apply_call_guards(call, fast_call, message)
    router.record_pick(config, pick_task, message, call[0] if call else None, fast=fast_call is not None)

    precompute = _weather_fast_line(call)
    if precompute is not None:
        logger.info("weather fast path: serving the precomputed line, no skill/phrasing calls")
        return ChatResult(
            reply=_said(config, session_id, precompute["line"], speaker),
            animation_tag=precompute["animation_tag"],
        )

    if call is None:
        # No tool (or a guard dropped the model's pick): the text is the
        # reply. A dropped pick usually has no text, so ask once more
        # without tools.
        reply = first.text.strip() if first is not None else ""
        if not reply:
            plain = await _reply_turn(prefix, config, speaker)
            reply = plain.text.strip()
        return ChatResult(reply=_said(config, session_id, reply, speaker))

    skill_name = call[0]
    result, tool_output = await _run_skill_call(call, registry, config, message)
    if speaker is not None:  # WP-65: the hush / reaction goes out with her first words
        early = _skill_chat_result("", result, skill_name, registry)
        speaker.tell(ReplyMeta(ui_action=early.ui_action, animation_tag=early.animation_tag))

    reply = ""
    steps = [(call, tool_output)]
    if tool_call is not None and first is not None:
        # the guard caught the claim only after part of it was said
        late = claimed and speaker is not None and bool(speaker.said)
        followup = prefix
        assistant_text = first.text
        while True:
            more = len(steps) < _MAX_TOOL_STEPS
            only_looked = all(done[0] in _LOOKUP_SKILLS for done, _ in steps)
            check = claims_an_action if more and only_looked else None
            tool_message = _tool_result_instruction(tool_output)
            if more:
                tool_message += "\n\n" + _CHAIN_NOTE
            if late and speaker is not None:
                tool_message += "\n\n" + correction_note(speaker.said_text())
            followup = [
                *followup,
                {"role": "assistant", "content": assistant_text, "tool_calls": [tool_call]},
                {"role": "tool", "tool_call_id": str(tool_call.get("id") or ""), "content": tool_message},
            ]
            said_before = len(speaker.said) if speaker is not None else 0
            nxt = await _reply_turn(followup, config, speaker, tools=tools, guard=check)
            late = False
            if check is not None and not nxt.tool_calls and check(nxt.text):
                # cm-p6a: "I've got it set for you" with no set_reminder call
                logger.warning("follow-up claimed an action without a tool call - retrying with a tool required")
                forced = await _forced_tool_turn(followup, tools, config)
                if forced is not None:
                    nxt = forced
                    late = speaker is not None and len(speaker.said) > said_before
            next_call = None
            if more and nxt.tool_calls:
                name, params = local_llm.parse_tool_arguments(nxt.tool_calls[0])
                next_call = _apply_call_guards((name, params) if name else None, None, message)
            if next_call is None:
                reply = "" if nxt.tool_calls else nxt.text.strip()
                break
            if any(next_call == done for done, _ in steps):
                logger.warning(
                    "she repeated %s with the same arguments - answering with what she has",
                    next_call[0],
                )
                break
            tool_call, assistant_text = nxt.tool_calls[0], nxt.text
            skill_name = next_call[0]
            result, tool_output = await _run_skill_call(next_call, registry, config, message)
            steps.append((next_call, tool_output))
    if not reply:
        # The presence fast path (no model turn yet), an empty phrasing
        # call, or a tool chain cut short: the shared plain phrasing step,
        # told everything that was really done.
        if len(steps) > 1:
            tool_output = (
                "\n".join(f"{done[0]}: {output}" for done, output in steps)
                + "\nOnly these steps were done - don't say anything else was."
            )
        async with metrics.timed("reply", route="local", model=config.local_llm_model):
            reply = await phrase_skill_result(message, tool_output, history, config, tone)
    reply = _said(config, session_id, reply, speaker)
    return _skill_chat_result(reply, result, skill_name, registry)


async def _summarize_mentioned_emails(config: FeltConfig, session_id: str, message: str, mails: list[dict]) -> str:
    # Same shape as every other local reply (personality prompt + the
    # spoken-length cap). No facts: the email body is all the context needed.
    async with metrics.timed("reply", route="local", model=config.local_llm_model):
        summary = await local_reply(
            [{"role": "user", "content": emails.summary_prompt(mails)}],
            config,
            include_facts=False,
        )

    emails.mark_summarised(config.db_path, [m["id"] for m in mails])
    save_message(config.db_path, session_id, "user", message)
    save_message(config.db_path, session_id, "assistant", summary)
    return summary


HER_LINES_N = 6


def her_lines_note(lines: list[dict]) -> str:
    """Build the note of her recent unprompted lines that goes into the history."""
    listed = "\n".join(f"- {line['content']}" for line in lines)
    return (
        "Things you said on your own recently, outside this conversation (he may "
        "bring them up; don't repeat them or reuse their opening words):\n" + listed
    )


async def chat(
    message: str,
    config: FeltConfig,
    session_id: str = "default",
    tone: str | None = None,
    answering: str | None = None,
    speaker: ReplySpeaker | None = None,
) -> ChatResult:
    """Answer one message from Mario and return her reply."""
    # Checked before anything else, including history/facts loading — a
    # bare "yes" / "tell me more" answering her mail mention (emails.py) has
    # to be caught here or it falls through to a generic chat reply that
    # doesn't know any email exists.
    if _looks_affirmative(message) and (mails := emails.just_mentioned(config.db_path)):
        summary = await _summarize_mentioned_emails(config, session_id, message, mails)
        return ChatResult(reply=summary)

    registry = features.enabled_skills(config.db_path, load_plugins(config.plugins_dir))
    # Only the conversation itself (Mario 10-03): with a remark every couple
    # of minutes at the desk, her own screen chatter used to push the real
    # back-and-forth out of the 20 lines. Her recent unprompted lines come
    # in as one note instead, so he can bring them up and she doesn't
    # repeat them.
    history = get_history(config.db_path, session_id, exclude_remarks=True)
    her_lines = get_her_recent_lines(config.db_path, session_id, limit=HER_LINES_N)
    if her_lines:
        history = [*history, {"role": "system", "content": her_lines_note(her_lines)}]

    # WP-02: "what do you think of this?" needs something to refer to.
    # Only when fresh enough to still be "current" - a stale observation
    # would be a stale, misleading answer.
    current_screen = get_current_screen_description(config.db_path, session_id, max_age_seconds=120)
    if current_screen:
        history = [*history, {"role": "system", "content": f"Currently on Mario's screen: {current_screen}"}]

    # WP-42.2: he tapped one of her asides in the app to answer it; without
    # this, a reply like "a bit, yeah" lands with nothing to refer to.
    if answering:
        history = [
            *history,
            {"role": "system", "content": f'Mario is answering this remark you made earlier: "{answering}"'},
        ]

    save_message(config.db_path, session_id, "user", message)
    # any message from him, on any device, can start her day (diary.py)
    from felt_core.core.diary import he_is_here

    he_is_here(config)

    from felt_core.core import appointment_hold

    try:
        return await _chat_local(message, config, session_id, tone, registry, history, speaker)
    finally:
        # an appointment she just asked "where?" about: his 30 s start now (cm-a4b)
        appointment_hold.start_wait(config)


async def chat_streamed(
    message: str,
    config: FeltConfig,
    session_id: str = "default",
    tone: str | None = None,
    answering: str | None = None,
) -> AsyncIterator[str | ReplyMeta | ChatResult]:
    """chat(), streamed (WP-65): yields her reply in sentence chunks.

    (as written, not yet stripped for TTS) the moment each one is complete -
    preceded by a ReplyMeta when the reply has a ui_action / animation tag
    known before her first words - and finally the whole ChatResult, the
    same one chat() would return, its `reply` being everything the chunks
    said. Same turn, same tools, guards and history (written once, at the
    end); only the felt-llm calls that can be her answer stream. Raises
    LocalLLMUnavailableError like chat(), possibly after some chunks.
    """
    speaker = ReplySpeaker(config)
    turn = asyncio.create_task(chat(message, config, session_id, tone, answering, speaker))
    turn.add_done_callback(lambda _: speaker.chunks.put_nowait(None))
    try:
        while (chunk := await speaker.chunks.get()) is not None:
            yield chunk
        result = turn.result()
        if not speaker.said and not speaker.told:  # e.g. the weather fast line's tag
            speaker.tell(ReplyMeta(ui_action=result.ui_action, animation_tag=result.animation_tag))
            while not speaker.chunks.empty():
                if (meta := speaker.chunks.get_nowait()) is not None:
                    yield meta
        for chunk in speaker.rest(result.reply):
            yield chunk
        yield result
    finally:
        turn.cancel()
