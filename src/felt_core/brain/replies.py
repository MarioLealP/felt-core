"""Reply generation on felt-llm (the only model backend, WP-36 D''), and
the helpers every brain submodule shares for an actual model-generated
line. This is the one module events.py imports from directly instead of
reaching into orchestrator internals (`local_reply`/`music_taste_context`
below are public on purpose). Every function here raises
`local_llm.LocalLLMUnavailable` when felt-llm is down - there is no
fallback, callers let it propagate (the API maps it to a 503)."""

import random
from datetime import date

from felt_core import local_llm
from felt_core.brain.persona import compile_persona

# Re-exported (not just used internally) so callers that already hold
# `felt_core.brain.replies` - tests, mainly - can read the constant from
# here too, without a second import of .personality.
from felt_core.brain.personality import PERSONALITY_PROMPT as PERSONALITY_PROMPT
from felt_core.brain.personality import personality_prompt
from felt_core.config import FeltConfig
from felt_core.db import get_all_facts, get_music_taste_stats
from felt_core.music_taste import summarize_taste
from felt_core.plugin_loader import LoadedSkill
from felt_core.self_memory import memory_block

# Every reply is spoken aloud, not read - a long reply means a long, rambly
# TTS clip (2026-09-22 finding: a weather reply's ~0.8 MB WAV, 12s of TTS
# alone out of a 33s turn). The generation-length cap, not a client-side
# truncation that could cut a sentence in half mid-word - the personality
# prompt's own "lead with the answer" instruction is what keeps a capped
# reply from feeling chopped off.
_REPLY_MAX_TOKENS = 160


def skill_tools(registry: dict[str, LoadedSkill]) -> list[dict]:
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


def _facts_context(config: FeltConfig) -> list[dict]:
    facts = get_all_facts(config.db_path)
    if not facts:
        return []
    bullet_list = "\n".join(f"- {fact}" for fact in facts)
    return [{"role": "system", "content": f"Known facts about the user:\n{bullet_list}"}]


def music_taste_context(config: FeltConfig) -> list[dict]:
    """WP-16 Part 2 - the listening-taste note. Empty until enough listening
    history exists to say anything (see music_taste.summarize_taste).
    Public (not underscore-prefixed): events.py's event reactions use it
    too (WP-40)."""
    note = summarize_taste(get_music_taste_stats(config.db_path))
    if not note:
        return []
    return [{"role": "system", "content": note}]


def _daily_persona() -> str:
    """The compiled persona with its example rotation seeded by the date
    (WP-36): the same prompt all day, so felt-llm's prompt cache keeps it,
    and a fresh sample tomorrow, so the examples still rotate."""
    return compile_persona(rng=random.Random(date.today().toordinal()))


def local_turn_messages(
    message: str, history: list[dict], config: FeltConfig, tone: str | None
) -> list[dict]:
    """The cache-stable layout for a local-first chat turn (WP-36, measured
    2026-09-29): felt-llm's template renders the tool declarations right
    AFTER the system text, so anything per-turn in the system message
    (tone, the memory block ranked on this message, facts, the current
    screen) invalidated ~5K cached tokens, persona + 29 tools, every turn
    (~20 s of prompt processing on the P104). The system message is now the
    day's fixed persona only; everything that changes per turn rides on the
    latest user message, after the history, where only it gets processed."""
    context: list[str] = []
    block = memory_block(config, message)
    if block:
        context.append(block)
    context += [m["content"] for m in _facts_context(config) + music_taste_context(config)]
    # chat() appends the fresh screen description as a system entry.
    context += [m["content"] for m in history if m.get("role") == "system"]
    if tone:
        context.append(f"Right now: {tone}")
    conversation = [m for m in history if m.get("role") != "system"]
    user = message
    if context:
        joined = "\n\n".join(context)
        user = (
            f"[Background for this reply, not said by Mario]\n{joined}\n"
            f"[End of background]\n\n{message}"
        )
    return (
        [{"role": "system", "content": _daily_persona()}]
        + conversation
        + [{"role": "user", "content": user}]
    )


async def local_reply(
    messages_tail: list[dict], config: FeltConfig, *, include_facts: bool, tone: str | None = None
) -> str:
    """A plain (no tools) felt-llm reply. Always carries the personality
    prompt; facts (Mario's remembered personal info) and music taste are
    opt-in per caller, since screen-reaction and event lines don't need
    them (WP-02: "Facts are not needed; don't include them"). Public: the
    orchestrator, the screen-reaction module and events.py all call this
    directly (WP-40). Raises LocalLLMUnavailable when felt-llm is down."""
    facts = _facts_context(config) if include_facts else []
    taste = music_taste_context(config) if include_facts else []
    context_text = messages_tail[-1]["content"] if messages_tail else None
    messages = (
        [{"role": "system", "content": personality_prompt(tone, context_text, config)}]
        + facts
        + taste
        + messages_tail
    )
    reply = await local_llm.chat(messages, max_tokens=_REPLY_MAX_TOKENS, config=config)
    return reply.text


def _tool_result_instruction(tool_output: str) -> str:
    """Shared by every phrasing call site (the tool-call follow-up, the
    presence fast path's phrasing, and the weather precompute) so they all
    give the model the identical instruction."""
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


async def phrase_skill_result(
    message: str,
    tool_output: str,
    history: list[dict],
    config: FeltConfig,
    tone: str | None = None,
) -> str:
    """Phrase a skill's result as a spoken reply (a plain felt-llm call).
    Used where there's no tool-call turn to follow up on - the presence
    fast path, or a model turn that produced no usable text."""
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
