"""Felt's character sheet (personality plan P1, 2026-09-27): loads
persona.yaml and compiles it into the system prompt.

Replaces the one-paragraph adjective persona. Adjectives without content made
every model fall back to its generic "friendly assistant", which is why she
agreed with everything and cheered in the same words. The sheet gives her
actual opinions, rules and example exchanges. The examples are what shape her
voice most, per the SillyTavern community's Ali:Chat practice.

Examples rotate: each compile draws a small random sample, not all of them,
so no single example turns into a template she copies (a known failure
when every call sees the same few-shot set).
"""

from __future__ import annotations

import random
from functools import cache
from pathlib import Path
from typing import Any

import yaml

PERSONA_PATH = Path(__file__).with_name("persona.yaml")

# The spoken-output contract. It's not personality, but every reply needs
# it, so it's appended to the compiled sheet (was the tail of the old
# PERSONALITY_PROMPT).
_SPOKEN_RULES = (
    "Everything you say is spoken aloud by a voice, never shown as text: "
    "no markdown, no lists, no emoji, no URLs; say numbers and units the "
    "way a person would out loud; lead with the answer. Keep it "
    "conversational: short for banter, longer only when the topic needs it."
)

EXAMPLES_PER_PROMPT = 4
# pt-PT lines always ride along (Mario 2026-10-21): rules alone left Brazilian
# forms in her Portuguese; examples shape her more than rules do.
PT_EXAMPLES_PER_PROMPT = 3


@cache
def load_persona(path: Path = PERSONA_PATH) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "identity" not in data:
        raise ValueError(f"{path} is not a persona sheet (no 'identity')")
    return data


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {' '.join(str(i).split())}" for i in items)


def _example_lines(examples: list[dict[str, str]]) -> str:
    out = []
    for ex in examples:
        lead = f"Mario: {ex['mario']}" if "mario" in ex else f"({ex['context']})"
        out.append(f"{lead}\nFelt: {ex['felt']}")
    return "\n\n".join(out)


def compile_persona(
    persona: dict[str, Any] | None = None,
    rng: random.Random | None = None,
    n_examples: int = EXAMPLES_PER_PROMPT,
) -> str:
    """The full system prompt for who she is. `rng` picks the rotating
    example sample; pass `random.Random(0)` for a stable prompt (tests,
    and the PERSONALITY_PROMPT constant)."""
    p = persona if persona is not None else load_persona()
    r = rng if rng is not None else random.Random()
    examples = p.get("examples", [])
    sample = r.sample(examples, min(n_examples, len(examples)))
    examples_pt = p.get("examples_pt", [])
    sample += r.sample(examples_pt, min(PT_EXAMPLES_PER_PROMPT, len(examples_pt)))

    sections = [
        " ".join(p["identity"].split()),
        " ".join(p.get("self_awareness", "").split()),
        " ".join(p.get("relationship", "").split()),
        "How you are:\n" + _bullets(p.get("temperament", [])),
        "Your opinions (bring them up when the topic is live, not as filler):\n"
        + _bullets(p.get("opinions", [])),
        "You like: " + ", ".join(p.get("likes", [])) + ".",
        "You dislike: " + ", ".join(p.get("dislikes", [])) + ".",
        "How you talk:\n" + _bullets(p.get("speech", [])),
        "Rules you always follow:\n" + _bullets(p.get("rules", [])),
        "A few examples of your voice. Match the spirit and never copy the "
        "wording:\n\n" + _example_lines(sample),
        _SPOKEN_RULES,
    ]
    return "\n\n".join(s for s in sections if s.strip())
