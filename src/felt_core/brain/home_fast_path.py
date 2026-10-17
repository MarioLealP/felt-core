"""WP-47: a plain home-control command goes straight to smart_home_control.

felt-llm doesn't reliably call the tool for "turn off the shelf light" - live
2026-10-17 she twice said she'd done it, then "I already flipped that switch",
and Home Assistant never saw a call. Like the presence fast path (WP-09), a
clear command skips the model's pick; she still phrases the result, and the
skill still only touches its allowlist. A status question only takes this path
when it names a kind of device ("is the fan on?"), so "is the server on fire"
still goes to the model.
"""

import re

_POLITE = r"(?:(?:hey|ok(?:ay)?|felt)[,!]?\s+)?(?:(?:can|could|would|will) you\s+)?(?:please\s+)?"
_TAIL = r"(?:\s*,?\s*(?:please|pls|plz|for me|thanks|thank you|now))*[\s.!?:)(]*$"
# a device name, never a second request ("the light and dance")
_DEVICE = r"(?P<device>[a-z0-9](?:(?!\band\b)[a-z0-9 '-]){0,40}?)"

# "turn off the shelf light", "switch on the fan please"
_VERB_FIRST = re.compile(
    rf"^\s*{_POLITE}(?:turn|switch|put)\s+(?P<action>on|off)\s+{_DEVICE}{_TAIL}", re.IGNORECASE
)
# "turn the shelf light off", "switch the lights back on"
_VERB_AROUND = re.compile(
    rf"^\s*{_POLITE}(?:turn|switch|put)\s+{_DEVICE}\s+(?:back\s+)?(?P<action>on|off){_TAIL}",
    re.IGNORECASE,
)
# "is the fan on?", "are the lights off"
_STATUS = re.compile(rf"^\s*(?:is|are)\s+{_DEVICE}\s+(?:still\s+)?(?:on|off){_TAIL}", re.IGNORECASE)
_KIND_WORDS = re.compile(r"\b(fans?|lights?|lamps?|plugs?|switch(?:es)?)\b", re.IGNORECASE)


def home_fast_path(message: str) -> tuple[str, dict] | None:
    for pattern in (_VERB_FIRST, _VERB_AROUND):
        m = pattern.match(message)
        if m:
            return "smart_home_control", {
                "device": m["device"].strip(),
                "action": m["action"].lower(),
            }
    m = _STATUS.match(message)
    if m and _KIND_WORDS.search(m["device"]):
        return "smart_home_control", {"device": m["device"].strip(), "action": "status"}
    return None
