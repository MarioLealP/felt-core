"""WP-09: Mario-initiated presence control - "shut up" hushes her (bubble-
only unprompted reactions for a while) and "you can talk again" undoes it.
Deterministic on the actual desktop side too - see brain.py's
`_PRESENCE_FAST_PATH_RE` for the regex fast path that bypasses the router
LLM entirely for these exact phrases, and felt-tray's app.py for where
ui_action.kind == "presence" actually calls felt-animation's /hush or
/unhush.
"""

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult, UIAction

name = "felt_presence"
description = (
    "Mario telling Felt to stop talking for a while ('shut up', 'shh', 'be quiet', "
    "'quiet please', 'stop talking') or to resume ('you can talk again', 'ok talk'). "
    "NOT for anything else - only this specific presence toggle."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "description": "'shut_up' to hush her, 'unhush' to let her talk again",
        },
    },
}

_HUSH_MINUTES = 10


async def handle(params, ctx):
    intent = str(get_single_param(params, "intent") or "shut_up").lower()

    if "unhush" in intent or "talk" in intent:
        return SkillResult(
            text="Okay, I'm back.",
            ui_action=UIAction(kind="presence", payload={"action": "unhush"}),
        )

    return SkillResult(
        text="okay okay, I'll zip it",
        ui_action=UIAction(kind="presence", payload={"action": "hush", "minutes": _HUSH_MINUTES}),
    )
