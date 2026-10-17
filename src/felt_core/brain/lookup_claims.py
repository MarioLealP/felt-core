"""FELT-108: spotting a reply that *says* she's looking something up - or
doing something - without a tool call.

felt-llm sometimes answers a tool-worthy question with "Checking that for you,
just a sec while I pull up the wiki... Okay, so a Chaos Orb is..." and no tool
call - the lookup never happened and the answer is from memory (2026-10-07,
both live tests). The orchestrator retries such a turn with a tool required.
Only the opening of the reply counts: that's where the claim sits, and a
later "let me know if..." isn't one.

Actions too (2026-10-17): "Consider it done. I'm flipping that switch for
you now" - and the shelf light never changed (no smart_home_control call).
A switching verb only counts with an on/off/up/down a few words later, so
"I'm going to turn in for the night" isn't a claim (past tense: on/off only,
since "it turned up" is everyday English).
"""

import re

_OPENING_CHARS = 160

_CLAIM = re.compile(
    r"\b("
    r"(let me|lemme|i'?ll|i will|i'?m going to|gonna|while i|one sec,? i'?ll)"
    r"\s+(just\s+|quickly\s+|go\s+)?"
    r"(check|look|search|pull up|dig|find|grab|peek|consult|run a search)"
    r"|checking (that|this|it|for you|now|my|your|the)"
    r"|looking (that|this|it) up"
    r"|pulling (up|that up|it up)"
    r"|searching (for|my|your|the|through)"
    r")",
    re.IGNORECASE,
)


_ACTION = re.compile(
    r"\b("
    r"consider it done"
    r"|(turn|switch|flip|toggle|shut|power|dimm?)(ing)?\b[^.!?]{0,40}?\b(on|off|up|down)\b"
    # past tense only with on/off: "the package turned up" is no claim
    r"|(turned|switched|flipped|toggled|powered|dimmed)\b[^.!?]{0,40}?\b(on|off)\b"
    r"|flipp?(ing|ed)? (that|the|this) switch"
    r")",
    re.IGNORECASE,
)


def claims_a_lookup(text: str) -> bool:
    return bool(_CLAIM.search((text or "")[:_OPENING_CHARS]))


def claims_an_action(text: str) -> bool:
    return bool(_ACTION.search((text or "")[:_OPENING_CHARS]))


def claims_a_tool_use(text: str) -> bool:
    """A lookup or an action she says she's doing - either needs a tool call."""
    return claims_a_lookup(text) or claims_an_action(text)
