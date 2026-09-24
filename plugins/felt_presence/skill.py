"""WP-09/WP-10: Mario-initiated presence control - hush/unhush (WP-09), plus
hide/brb/come-back/come/move/look/repeat (WP-10). One skill, one UIAction
kind ("presence") for everything here - felt-tray's app.py dispatches on
`payload["action"]` to the matching felt-animation call (/hush, /unhush,
/presence, /repeat) or, for "look_at_this", its own local manual-capture
trigger rather than any felt-animation call at all.

Deterministic on the actual desktop side too for the two time-critical
WP-09 phrases specifically - see brain.py's `_presence_fast_path`, which
bypasses the router LLM entirely for "shut up"/"you can talk again" so
neither waits on nor risks a misroute from it. The WP-10 intents added
here go through the normal router (see router.py's worked example) - none
of them are as time-critical as being told to shut up mid-sentence.
"""

import logging

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult, UIAction

name = "felt_presence"
description = (
    "Mario controlling Felt's own presence/behavior on his desktop: telling her to "
    "stop talking ('shut up', 'shh', 'be quiet', 'quiet please', 'stop talking') or "
    "resume ('you can talk again', 'ok talk'); go away for a while ('go away for a "
    "bit') or for a set time ('give me five minutes'); come back ('come back'); walk "
    "to a specific spot ('come here', 'move over'); react to what's on screen right "
    "now ('look at this'); or repeat her last line ('what did you say?'). NOT for "
    "anything else - only these specific presence/behavior controls."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "description": "one of: shut_up, unhush, hide, brb, come_back, come, move, look, repeat",
        },
    },
}

# canonical intent -> (spoken line, felt-tray ui_action payload)
_INTENT_REPLIES = {
    "shut_up": ("okay okay, I'll zip it", {"action": "hush", "minutes": 10}),
    "unhush": ("Okay, I'm back.", {"action": "unhush"}),
    "hide": ("Okay, I'll get out of your way.", {"action": "hide"}),
    "brb": ("Be right back!", {"action": "brb"}),
    "come_back": ("Coming!", {"action": "summon"}),
    "come": ("On my way.", {"action": "come"}),
    "move": ("Okay, moving.", {"action": "move"}),
    "look": ("Let me see...", {"action": "look_at_this"}),
    "repeat": ("Sure, one more time.", {"action": "repeat"}),
}

# A router-guessed phrase rarely lands on the exact canonical slug above -
# these are the literal example phrasings from WP-10's own decisions list,
# normalized to underscores the same way _canonical_intent() normalizes
# whatever it's given.
_INTENT_ALIASES = {
    "shh": "shut_up",
    "be_quiet": "shut_up",
    "quiet_please": "shut_up",
    "stop_talking": "shut_up",
    "ok_talk": "unhush",
    "you_can_talk_again": "unhush",
    "go_away_for_a_bit": "hide",
    "give_me_five_minutes": "brb",
    "come_here": "come",
    "move_over": "move",
    "shoo": "move",
    "look_at_this": "look",
    "what_did_you_say": "repeat",
    "say_that_again": "repeat",
}

# Last-resort substring match for anything neither table catches exactly -
# order matters (checked top to bottom, first match wins), most-specific
# phrases first so e.g. "come back" doesn't get caught by a bare "come".
_FALLBACK_SUBSTRINGS = [
    ("talk again", "unhush"),
    ("unhush", "unhush"),
    ("stop talking", "shut_up"),
    ("shut up", "shut_up"),
    ("quiet", "shut_up"),
    ("shh", "shut_up"),
    ("five minutes", "brb"),
    ("minutes", "brb"),
    ("brb", "brb"),
    ("come back", "come_back"),
    ("come here", "come"),
    ("come", "come"),
    ("move", "move"),
    ("shoo", "move"),
    ("what did you say", "repeat"),
    ("say that again", "repeat"),
    ("repeat", "repeat"),
    ("look", "look"),
    ("go away", "hide"),
    ("hide", "hide"),
]


logger = logging.getLogger(__name__)

# What she says when the router sent something here that isn't a presence
# control at all - nothing changes, and the phrasing step turns it into a
# natural "huh?" rather than silence.
_UNKNOWN_INTENT_TEXT = "That didn't match any of my presence controls, so nothing changed."


def _canonical_intent(raw: str) -> str | None:
    key = raw.strip().lower().replace(" ", "_")
    if key in _INTENT_REPLIES:
        return key
    if key in _INTENT_ALIASES:
        return _INTENT_ALIASES[key]
    spaced = key.replace("_", " ")
    for phrase, canonical in _FALLBACK_SUBSTRINGS:
        if phrase in spaced:
            return canonical
    # No match: None, never a guess. This used to default to shut_up,
    # which let an LLM misroute hush her - against the "never truly
    # quiet" rule (only Mario may hush her; audit 2026-09-24).
    return None


async def handle(params, ctx):
    raw_intent = str(get_single_param(params, "intent") or "")
    intent = _canonical_intent(raw_intent) if raw_intent.strip() else None
    if intent is None:
        logger.warning("felt_presence: unknown/missing intent %r - taking no action", raw_intent)
        return SkillResult(text=_UNKNOWN_INTENT_TEXT)
    text, payload = _INTENT_REPLIES[intent]
    return SkillResult(text=text, ui_action=UIAction(kind="presence", payload=payload))
