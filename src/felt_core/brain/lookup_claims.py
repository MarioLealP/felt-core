"""FELT-108: spotting a reply that *says* she's looking something up.

felt-llm sometimes answers a tool-worthy question with "Checking that for you,
just a sec while I pull up the wiki... Okay, so a Chaos Orb is..." and no tool
call - the lookup never happened and the answer is from memory (2026-10-07,
both live tests). The orchestrator retries such a turn with a tool required.
Only the opening of the reply counts: that's where the claim sits, and a
later "let me know if..." isn't one.
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


def claims_a_lookup(text: str) -> bool:
    return bool(_CLAIM.search((text or "")[:_OPENING_CHARS]))
