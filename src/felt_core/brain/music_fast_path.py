"""WP-48: plain music commands go straight to the Spotify skills, like the
home fast path - felt-llm's tool pick isn't reliable for one-word commands
("skip"), and a wrong pick here would be a surprise in Mario's ears. Only
whole-message commands count; bare "stop" stays the presence hush.
"""

import re

from felt_core.brain.command_words import POLITE, TAIL

_MUSIC = r"(?:the\s+)?(?:music|song|track|spotify|this(?:\s+(?:song|one|track))?|it)"
# the music by name only - "stop it" / "turn it off" could mean her
_NAMED = r"(?:the\s+)?(?:music|song|spotify)"


def _cmd(body: str) -> re.Pattern:
    return re.compile(rf"^\s*{POLITE}(?:{body}){TAIL}", re.IGNORECASE)


_COMMANDS: list[tuple[re.Pattern, str, dict]] = [
    (
        _cmd(rf"skip(?:\s+{_MUSIC})?|next(?:\s+(?:song|track|one))?"),
        "media_control",
        {"action": "next"},
    ),
    (
        _cmd(r"(?:go\s+)?back(?:\s+a\s+song)?|previous(?:\s+(?:song|track|one))?|last\s+song"),
        "media_control",
        {"action": "previous"},
    ),
    (
        # "stop it" is left alone (more likely aimed at her than the music)
        _cmd(
            rf"pause(?:\s+{_MUSIC})?|stop\s+{_NAMED}"
            rf"|turn\s+off\s+{_NAMED}|turn\s+{_NAMED}\s+off"
        ),
        "media_control",
        {"action": "pause"},
    ),
    (
        _cmd(
            rf"(?:resume|unpause)(?:\s+{_MUSIC})?|keep\s+playing|play\s+(?:the\s+)?music"
            rf"|turn\s+on\s+{_NAMED}|turn\s+{_NAMED}\s+(?:back\s+)?on"
        ),
        "media_control",
        {"action": "resume"},
    ),
    (
        _cmd(rf"(?:turn\s+{_MUSIC}\s+up|louder|volume\s+up|turn\s+up\s+{_MUSIC})"),
        "media_control",
        {"action": "louder"},
    ),
    (
        _cmd(rf"(?:turn\s+{_MUSIC}\s+down|quieter|softer|volume\s+down|turn\s+down\s+{_MUSIC})"),
        "media_control",
        {"action": "quieter"},
    ),
    (
        _cmd(
            r"(?:save|like|heart)\s+(?:this|the|that)\s*(?:song|track|one)?"
            r"(?:\s+to\s+(?:my\s+)?(?:library|liked\s+songs))?"
        ),
        "media_control",
        {"action": "save"},
    ),
    (
        _cmd(r"what(?:'s|\s+is)\s+(?:this|that)\s+song|what\s+song\s+is\s+(?:this|that|playing)"),
        "now_playing",
        {},
    ),
]
_VOLUME = re.compile(
    rf"^\s*{POLITE}(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+)?(?P<n>\d{{1,3}})\s*%?{TAIL}",
    re.IGNORECASE,
)


def music_fast_path(message: str) -> tuple[str, dict] | None:
    m = _VOLUME.match(message)
    if m:
        return "media_control", {"action": "volume", "level": int(m["n"])}
    for pattern, skill, params in _COMMANDS:
        if pattern.match(message):
            return skill, dict(params)
    return None
