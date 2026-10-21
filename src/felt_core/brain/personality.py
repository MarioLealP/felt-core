"""The personality system prompt applied to every reply - chat or screen
reaction. Its own module (not folded into replies.py or
orchestrator.py) because every other brain submodule needs it: chat
orchestration, reply generation, screen reactions, and the background
precompute loops all carry it as their system framing - a leaf module
keeps none of them depending on each other just to get it.

Since 2026-09-27 (personality plan) who she is comes from persona.yaml,
compiled by brain/persona.py, not from a paragraph of adjectives. Eval
run 2 showed why: the sheet disagrees where the old prompt half-agreed, it
talks about "my legs" where the old one called her "him", and it drops the
bit when Mario is tired. Kept distinct from facts_context (personal facts about
Mario) - this is just who she is.
"""

import random
from datetime import date
from typing import TYPE_CHECKING

from felt_core.brain.persona import compile_persona
from felt_core.self_memory import memory_block

if TYPE_CHECKING:
    from felt_core.config import FeltConfig

# A stable compile (fixed example sample) for the call sites that import the
# constant (the email summary, background precompute). Live replies use
# daily_persona(), whose examples rotate once a day.
PERSONALITY_PROMPT = compile_persona(rng=random.Random(0))


def daily_persona() -> str:
    """The system prompt for every felt-llm call: the compiled persona with
    its example rotation seeded by the date (WP-36) - the same text all day,
    so felt-llm caches it once for chat and every background line alike, and
    a fresh sample tomorrow. Until 2026-10-21 the background lines drew a
    fresh sample per call and carried the memory block and tone in here too,
    so felt-llm re-read the whole persona for each one (~12 s a screen
    reaction); what changes per call goes in the user turn now
    (per_call_context)."""
    return compile_persona(rng=random.Random(date.today().toordinal()))


def per_call_context(
    tone: str | None = None,
    context_text: str | None = None,
    config: "FeltConfig | None" = None,
) -> str:
    """What changes per call, for the user turn - never the system prompt.
    WP-15: `tone` is felt-animation's GET /tone line (mood x time-of-day +
    streak awareness), None when a call site doesn't carry one. WP-26/P4:
    with `config`, `self_memory.memory_block`'s budgeted "Recently (from your
    diary)" + "Who you've become" block, ranked against `context_text`
    (whatever's on screen/in the message right now) when given, else by
    pinned+recency alone; `config=None` skips it (no DB touched). "" when
    there's nothing."""
    parts = []
    if config is not None:
        block = memory_block(config, context_text)
        if block:
            parts.append(block)
    if tone:
        parts.append(f"Right now: {tone}")
    return "\n\n".join(parts)


def with_context(context: str, text: str) -> str:
    """A user turn's text with the per-call context ahead of it."""
    return f"{context}\n\n{text}" if context else text
