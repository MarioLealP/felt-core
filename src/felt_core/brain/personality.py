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
from typing import TYPE_CHECKING

from felt_core.brain.persona import compile_persona
from felt_core.self_memory import memory_block

if TYPE_CHECKING:
    from felt_core.config import FeltConfig

# A stable compile (fixed example sample) for the call sites that import the
# constant (the email summary, background precompute). Live replies go
# through personality_prompt(), which rotates the examples on every call.
PERSONALITY_PROMPT = compile_persona(rng=random.Random(0))


def personality_prompt(
    tone: str | None = None,
    context_text: str | None = None,
    config: "FeltConfig | None" = None,
) -> str:
    """WP-15: `tone` is felt-animation's GET /tone line (mood x time-of-day
    + streak awareness - felt-tray/felt-dashboard fetch it and attach it to
    /v1/react/screen, /v1/voice-chat, /v1/text-chat). None on an older
    client, felt-animation unreachable, or a call site that doesn't carry
    one (the pending-email summary, the weather precompute) - the compiled
    sheet alone is always a safe, complete system prompt on its own, this
    is purely additive. Each call draws a fresh example sample.

    WP-26 combined with personality plan P4: `config` (when a call site has
    one cheaply in scope - see replies.py/screen_reactions.py/events.py)
    additionally threads in `self_memory.memory_block`'s budgeted "Recently
    (from your diary)" + "Who you've become" block, ranked against
    `context_text` (whatever's on screen/in the message right now) when
    given, else by pinned+recency alone. `config=None` (a call site with no
    config cheaply in scope, or a test exercising the bare persona) skips
    the block entirely - no DB touched, no behaviour change from before."""
    base = compile_persona()
    parts = [base]
    if config is not None:
        block = memory_block(config, context_text)
        if block:
            parts.append(block)
    if tone:
        parts.append(f"Right now: {tone}")
    return "\n\n".join(parts)
