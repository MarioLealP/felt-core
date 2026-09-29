"""Passive screen reactions - two entry points, kept separate on purpose
(WP-40's audit: they were flagged as "duplicative" because both used to
inline their own copy of the exact same cooldown check, not because their
model calls are the same thing):

- process_screen_context(): the older felt-vision path. Takes a plain-text
  description, tries cloud text phrasing then falls back to local Ollama,
  same shape as every other reply in replies.py.
- react_to_screen(): WP-07's per-window path. Takes a JPEG straight from
  felt-tray, makes ONE multimodal cloud call for a JSON {seen, line,
  emotion} triple, and has NO local fallback (there's no local VLM on this
  server) - a CloudUnavailable propagates to the caller instead.

Merged here (WP-40): the shared "are we on cooldown" check
(_on_cooldown), pulled out of both bodies into one function - previously
each one inlined its own copy of the identical
seconds_since_last_spoken_reaction + logging + early-return dance.
test_screen_reaction_merge.py proves both entry points still behave
exactly as before against that shared helper.
"""

import json
import logging
import re

from felt_core import local_llm, metrics
from felt_core.brain.personality import personality_prompt
from felt_core.brain.replies import legacy_config, local_model_label, local_reply
from felt_core.brain.result import ChatResult
from felt_core.cloud import cloud_generate
from felt_core.config import FeltConfig
from felt_core.db import (
    get_history,
    get_recent_observations,
    save_message,
    save_observation,
    seconds_since_last_spoken_reaction,
    set_observation_reply,
)
from felt_core.privacy import is_safe_for_cloud

logger = logging.getLogger(__name__)

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


RECENT_LINES_N = 8


def recent_lines_block(lines: list[str]) -> str:
    """Her own recent remarks, fed back so she stops echoing herself
    (personality plan P3, B-1). It goes LAST, in the per-glance
    instruction, not in `system`: models weight what they read last most
    (the SillyTavern post-history lesson), and "don't repeat" loses to a
    long persona block otherwise."""
    if not lines:
        return ""
    listed = "\n".join(f"- {line}" for line in lines)
    return (
        "\n\nThings you already said recently (never repeat, rephrase, or reuse the "
        "opening words of any of these; if they were upbeat, don't cheer again):\n" + listed
    )


def _recent_lines(config: FeltConfig, session_id: str) -> list[str]:
    rows = get_recent_observations(config.db_path, session_id, limit=RECENT_LINES_N)
    return [r["spoken_reply"] for r in rows if r.get("spoken_reply")]


def _on_cooldown(config: FeltConfig, session_id: str) -> bool:
    """WP-40 merge: the one cooldown check both process_screen_context and
    react_to_screen use - previously each inlined an identical copy of
    this exact seconds_since_last_spoken_reaction + logging + early-return
    block. True means "don't call a model, we're on cooldown" (already
    logged when true)."""
    seconds_since = seconds_since_last_spoken_reaction(config.db_path, session_id)
    if seconds_since is not None and seconds_since < config.screen_reaction_cooldown_s:
        logger.info(
            "screen reaction: on cooldown (%.0fs since last spoken reaction, need %ds) - no model call",
            seconds_since,
            config.screen_reaction_cooldown_s,
        )
        return True
    return False


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

    if _on_cooldown(config, session_id):
        return ChatResult(reply="", animation_tag=None)

    observations_block = _observations_block(
        get_recent_observations(config.db_path, session_id, limit=3)
    )
    instruction = _SCREEN_REACTION_PROMPT + recent_lines_block(_recent_lines(config, session_id))

    async def _local_attempt() -> str:
        # Last 4 REAL chat messages, not facts - facts are personal-info
        # recall, irrelevant to reacting to what's on screen right now.
        # "Real" excludes her own earlier screen remarks (audit
        # 2026-09-24) - those are saved as assistant messages too.
        history_tail = get_history(config.db_path, session_id, exclude_remarks=True)[-4:]
        async with metrics.timed("screen_reaction", route="local", model=local_model_label(config)):
            return await local_reply(
                [{"role": "system", "content": observations_block}]
                + history_tail
                + [{"role": "user", "content": instruction}],
                config,
                include_facts=False,
                tone=tone,
            )

    reply_raw: str | None = None
    if config.local_first:
        # WP-36: felt-llm first, no privacy check (nothing leaves the LAN).
        # The description only goes to the cloud below if felt-llm is down
        # AND it passes the privacy check.
        try:
            reply_raw = await _local_attempt()
        except local_llm.LocalLLMUnavailable:
            logger.warning("screen context reaction: felt-llm unavailable, using the old path")
            config = legacy_config(config)

    if reply_raw is None:
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
                async with metrics.timed(
                    "screen_reaction", route="cloud", model=config.cloud_model_volume
                ):
                    reply_raw = await cloud_generate(
                        "screen_reaction",
                        f"{personality_prompt(tone, description, config)}\n\n{observations_block}",
                        [{"role": "user", "content": instruction}],
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
        try:
            reply_raw = (await _local_attempt()).strip(" \n\"'")
        except local_llm.LocalLLMUnavailable:
            config = legacy_config(config)
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
    'never mention screens or screenshots>", '
    '"emotion": "<one of: happy, excited, amused, sad, annoyed, surprised, '
    'thinking, sleepy, neutral>"}'
)


# WP-36: the same {seen, line, emotion} contract as _SCREEN_REACT_INSTRUCTION,
# but enforced by llama.cpp's grammar (response_format json_schema) instead of
# asked for in prose - emotion is constrained to the fixed set.
_SCREEN_REACT_SCHEMA = {
    "type": "object",
    "properties": {
        "seen": {"type": "string"},
        "line": {"type": "string"},
        "emotion": {"type": "string", "enum": sorted(_EMOTION_VALUES)},
    },
    "required": ["seen", "line", "emotion"],
    "additionalProperties": False,
}


async def _react_to_screen_local(
    jpeg_bytes: bytes, system: str, user_text: str, config: FeltConfig
) -> dict | None:
    """WP-36: the local-first attempt - felt-llm with the JPEG as an image
    part and the schema-constrained JSON reply. One retry on a parse
    failure; None (felt-llm unreachable, or two unparseable replies) sends
    the caller on to the cloud call."""
    for attempt in (1, 2):
        try:
            reply = await local_llm.chat(
                [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_text},
                            local_llm.image_part(jpeg_bytes),
                        ],
                    },
                ],
                max_tokens=200,
                json_schema=_SCREEN_REACT_SCHEMA,
                config=config,
            )
        except local_llm.LocalLLMUnavailable:
            logger.warning("screen reaction: felt-llm unavailable, using the cloud")
            return None
        parsed = _parse_screen_react_json(reply.text)
        if parsed is not None:
            return parsed
        logger.warning("screen reaction: local JSON parse failed (attempt %d)", attempt)
    return None


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
    """WP-36: with config.local_first the image goes to felt-llm first
    (schema-constrained JSON, route "local"); the cloud call described
    below is then only the fallback when felt-llm is unreachable or twice
    returns unparseable JSON.

    WP-07's new per-window, image-based screen reaction path. The
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
    if trigger != "manual" and _on_cooldown(config, session_id):
        return ChatResult(reply="", animation_tag=None)

    observations_block = _observations_block(
        get_recent_observations(config.db_path, session_id, limit=3)
    )
    system = f"{personality_prompt(tone, f'{app} {title}', config)}\n\n{observations_block}"
    context_line = f'App: {app} — "{title}"'
    recent = recent_lines_block(_recent_lines(config, session_id))

    parsed: dict | None = None
    if config.local_first:
        async with metrics.timed("screen_reaction", route="local", model=config.local_llm_model):
            parsed = await _react_to_screen_local(
                jpeg_bytes, system, f"{context_line}\n\n{_SCREEN_REACT_INSTRUCTION}{recent}", config
            )

    # WP-04's screen_reaction stage (the model call(s), cloud route) -
    # WP-05 had left only the generic `cloud` stage here (audit 2026-09-24).
    # Under local_first this is only the fallback (felt-llm down, or two
    # unparseable replies).
    if parsed is None:
        async with metrics.timed("screen_reaction", route="cloud", model=config.cloud_model_volume):
            raw = await cloud_generate(
                "screen_reaction",
                system,
                [
                    {
                        "role": "user",
                        "content": f"{context_line}\n\n{_SCREEN_REACT_INSTRUCTION}{recent}",
                    }
                ],
                config,
                images=[jpeg_bytes],
                json_mode=True,
            )
            parsed = _parse_screen_react_json(raw)

            if parsed is None:
                logger.warning(
                    "screen reaction: JSON parse failed on the first attempt, retrying once"
                )
                retry_raw = await cloud_generate(
                    "screen_reaction",
                    system,
                    [
                        {
                            "role": "user",
                            "content": f"{context_line}\n\n{_SCREEN_REACT_INSTRUCTION}{recent}\n\n"
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
