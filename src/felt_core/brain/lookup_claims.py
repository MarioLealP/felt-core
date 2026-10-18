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

Music too (2026-10-18 e2e run): "Playing some Bring Me The Horizon." and
"Paused the music." with nothing sent to Spotify. Tight on purpose - a false
alarm forces a tool call, which could pause his music: "Playing" only as the
reply's first word, the verbs only with the music (or it/that/this) after.
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
    r"|(turn|switch|flip|toggle|shut|power|dimm?|dial)(ing)?\b[^.!?]{0,40}?\b(on|off|up|down)\b"
    # past tense only with on/off: "the package turned up" is no claim
    r"|(turned|switched|flipped|toggled|powered|dimmed)\b[^.!?]{0,40}?\b(on|off)\b"
    r"|flipp?(ing|ed)? (that|the|this) switch"
    r")",
    re.IGNORECASE,
)

_MUSIC = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|sure|got it|alright|done|there)\W+)*(?:now\s+)?playing\b"
    r"(?!\s+(?:it\s+(?:cool|safe)|along|games?|with|around|dead|nice|fair)\b)"
    r"|\b(?:paused|skipped|queued(?:\s+up)?|resumed|pausing|skipping|queu(?:e)?ing(?:\s+up)?|resuming)"
    r"\s+(?:it|that|this|the\s+(?:music|song|track))\b"
    r"|\b(?:putting|throwing|popping)\s+on\s+(?:some|a\s+(?:song|track)|it|that|this)\b"
    r"|^\W*(?:(?:ok(?:ay)?|sure|got it|alright|done)\W+)*moving on\b"
    r"|\bmoving on to (?:something|the next|another)"
    r"|^\W*(?:(?:ok(?:ay)?|sure|got it|alright|done)\W+)*switching (?:gears|it up|things up)\b"
    r"|\b(?:adding|i'?ll add|added|i'?ve added)\b[^.!?]{0,40}?\b(?:to|in|into) (?:the|your) queue\b"
    r"|\b(?:it'?s|volume'?s|volume is)\s+(?:now\s+)?(?:at|down to|up to)\s+[\w-]+\s*(?:percent|%)"
    r"|\b(?:lowering|raising|dropping|bumping)\s+(?:the\s+)?(?:volume|it|music)\b",
    re.IGNORECASE,
)


def claims_a_lookup(text: str) -> bool:
    return bool(_CLAIM.search((text or "")[:_OPENING_CHARS]))


def claims_an_action(text: str) -> bool:
    opening = (text or "")[:_OPENING_CHARS]
    return bool(_ACTION.search(opening) or _MUSIC.search(opening))


def claims_a_tool_use(text: str) -> bool:
    """A lookup or an action she says she's doing - either needs a tool call."""
    return claims_a_lookup(text) or claims_an_action(text)
