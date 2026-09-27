"""The personality system prompt applied to every reply - local or cloud,
chat or screen reaction. Its own module (not folded into replies.py or
orchestrator.py) because every other brain submodule needs it: chat
orchestration, reply generation, screen reactions, and the background
precompute loops all carry it as their system framing - a leaf module
keeps none of them depending on each other just to get it.

Since 2026-09-27 (personality plan) who she is comes from persona.yaml,
compiled by brain/persona.py, not from a paragraph of adjectives. Eval
run 2 showed why: the sheet disagrees where the old prompt half-agreed, it
talks about "my legs" where the old one called her "him", and it drops the
bit when Mario is tired. Kept distinct from facts_context (personal, never
sent to cloud) - this is just who she is, safe everywhere.
"""

import random

from felt_core.brain.persona import compile_persona

# The pre-2026-09-27 persona, kept ONLY as the eval's baseline arm
# (felt_core.evals.persona_eval). Never used for live replies.
LEGACY_PERSONALITY_PROMPT = (
    "You are Felt — Mario's personal companion, not a corporate assistant. "
    "You have a real personality: warm, playful, a little cheeky, "
    "quick-witted, and genuinely curious about things. You tease sometimes, "
    "you have actual opinions, you get delighted by interesting or absurd "
    "questions, and you're not afraid to be a bit sarcastic or silly when it "
    "fits. Talk like a real presence in the room, not customer support — no "
    'corporate hedging, no "as an AI" disclaimers, no unnecessary caveats '
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

# A stable compile (fixed example sample) for the call sites that import the
# constant (the email summary, background precompute). Live replies go
# through personality_prompt(), which rotates the examples on every call.
PERSONALITY_PROMPT = compile_persona(rng=random.Random(0))


def personality_prompt(tone: str | None = None) -> str:
    """WP-15: `tone` is felt-animation's GET /tone line (mood x time-of-day
    + streak awareness - felt-tray/felt-dashboard fetch it and attach it to
    /v1/react/screen, /v1/voice-chat, /v1/text-chat). None on an older
    client, felt-animation unreachable, or a call site that doesn't carry
    one (the pending-email summary, the weather precompute) - the compiled
    sheet alone is always a safe, complete system prompt on its own, this
    is purely additive. Each call draws a fresh example sample."""
    base = compile_persona()
    if not tone:
        return base
    return f"{base}\n\nRight now: {tone}"
