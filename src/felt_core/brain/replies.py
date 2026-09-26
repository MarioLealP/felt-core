"""Reply generation - local (Ollama) and cloud (Gemini/Gemma), and the
skill-result phrasing step that routes a tool's output through one or the
other by its privacy class. Split out of brain.py (WP-40) as its own
module: this is the one piece every other brain submodule (chat
orchestration, screen reactions, the background loops) calls into for an
actual model-generated line, so it's also the one module events.py should
import from directly instead of reaching into brain.py's private helpers
(the WP-40 fix for that: `local_reply`/`music_taste_context` below are
public on purpose, not underscore-prefixed, for exactly that use)."""

import logging

import httpx

from felt_core import metrics

# Re-exported (not just used internally) so callers that already hold
# `felt_core.brain.replies` - tests, mainly - can read the constant from
# here too, without a second import of .personality.
from felt_core.brain.personality import PERSONALITY_PROMPT as PERSONALITY_PROMPT
from felt_core.brain.personality import personality_prompt
from felt_core.cloud import cloud_generate
from felt_core.config import FeltConfig
from felt_core.db import get_all_facts, get_music_taste_stats
from felt_core.music_taste import summarize_taste
from felt_core.privacy import is_safe_for_cloud

logger = logging.getLogger(__name__)

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


def _facts_context(config: FeltConfig) -> list[dict]:
    facts = get_all_facts(config.db_path)
    if not facts:
        return []
    bullet_list = "\n".join(f"- {fact}" for fact in facts)
    return [{"role": "system", "content": f"Known facts about the user:\n{bullet_list}"}]


def music_taste_context(config: FeltConfig) -> list[dict]:
    """WP-16 Part 2 - unlike _facts_context, this is fine on the cloud
    path too (Mario, 2026-09-22: music is explicitly cloud-ok, same as
    email/calendar), so both local_reply and _cloud_chat include it
    unconditionally. Empty until enough listening history exists to say
    anything (see music_taste.summarize_taste). Public (not underscore-
    prefixed): events.py's event reactions use it too (WP-40 - it used to
    reach straight into brain.py's private `_music_taste_context`)."""
    note = summarize_taste(get_music_taste_stats(config.db_path))
    if not note:
        return []
    return [{"role": "system", "content": note}]


async def local_reply(
    messages_tail: list[dict], config: FeltConfig, *, include_facts: bool, tone: str | None = None
) -> str:
    """The local (Ollama) reply path, factored out of three near-identical
    call sites (2026-09-22, WP-02) - plain chat's fallback, and both of
    screen-context's local branches. Always carries the personality
    prompt; facts (Mario's remembered personal info) are opt-in per
    caller, since screen-reaction replies don't need them (WP-02: "Facts
    are not needed; don't include them"). Public (not underscore-prefixed):
    the chat orchestrator, the screen-reaction module, and events.py's
    event reactions all call this directly (WP-40)."""
    # include_facts also gates music taste: both are "personal context a
    # plain-chat reply wants but a screen-reaction body-language line
    # doesn't" (WP-02's original reasoning for facts applies just the
    # same here), even though music taste - unlike facts - is allowed on
    # the cloud path too (see music_taste_context).
    facts = _facts_context(config) if include_facts else []
    taste = music_taste_context(config) if include_facts else []
    messages = (
        [{"role": "system", "content": personality_prompt(tone)}] + facts + taste + messages_tail
    )
    return await _ollama_chat(messages, config.ollama_model, config, max_tokens=_REPLY_MAX_TOKENS)


async def _cloud_chat(
    message: str, history: list[dict], config: FeltConfig, tone: str | None = None
) -> str:
    """Routes through cloud.cloud_generate's "conversation" purpose
    (WP-05) - NOT ninerouter/Antigravity, which contaminates every reply
    with its own coding-agent identity no matter what system prompt is
    supplied (see config.py's gemini_api_key comment). Carries a bounded
    recent-context window (CLOUD_CONTEXT_MESSAGES) and the personality
    prompt - never facts_context (personal information Mario has
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
    system = personality_prompt(tone)
    for note in music_taste_context(config):
        system = f"{system}\n\n{note['content']}"
    return await cloud_generate(
        "conversation",
        system,
        recent + [{"role": "user", "content": message}],
        config,
    )


async def _chat_reply(
    message: str,
    history: list[dict],
    config: FeltConfig,
    safe: bool | None,
    tone: str | None = None,
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
        return await local_reply(
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
                    "skill_phrasing",
                    personality_prompt(tone),
                    messages,
                    config,
                    max_tokens=_SKILL_PHRASING_MAX_TOKENS,
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
        return await local_reply(
            history
            + [
                {"role": "user", "content": message},
                {"role": "user", "content": _tool_result_instruction(tool_output)},
            ],
            config,
            include_facts=True,
            tone=tone,
        )
