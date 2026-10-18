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
        _cmd(
            rf"restart(?:\s+{_MUSIC})?|start\s+(?:{_MUSIC}\s+)?over"
            rf"|(?:play\s+)?{_MUSIC}\s+from\s+the\s+(?:start|beginning|top)"
        ),
        "media_control",
        {"action": "restart"},
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
# "play birdbrain jamie paige" -> play_music by name; tried after the fixed
# commands ("play the music" is resume). Not "play a game with me" & co.
_PLAY = re.compile(rf"^\s*{POLITE}play\s+(?:me\s+)?(?P<query>.{{2,80}}?){TAIL}", re.IGNORECASE)
_NOT_MUSIC = re.compile(
    r"^(?:a\s+)?(?:games?\b|with\b|along\b|nice\b|around\b|dead\b|chess\b|tag\b"
    r"|hide\b|pretend\b|fair\b|it\s+(?:cool|safe|by\s+ear)\b|on\b|for\b)",
    re.IGNORECASE,
)
_THIS = r"(?:this|that)(?:\s+(?:one|song|track))?"
# "more like this", "less of that", "fewer songs like this one"
_LIKE = rf"(?:(?:songs?|stuff|tracks?)\s+)?(?:like|of)\s+{_THIS}"
_DJ: list[tuple[re.Pattern, dict]] = [
    (
        _cmd(r"(?:turn\s+)?(?:the\s+)?dj\s+mode\s+off|stop\s+(?:the\s+)?dj(?:\s+mode)?|end\s+dj"),
        {"action": "stop"},
    ),
    (
        _cmd(
            r"(?:turn\s+on\s+)?(?:the\s+)?dj\s+mode(?:\s+on)?|be\s+my\s+dj|dj\s+for\s+me"
            r"|start\s+(?:the\s+)?dj(?:\s+mode)?"
        ),
        {"action": "start"},
    ),
    (_cmd(rf"(?:no\s+)?(?:less|fewer)\s+{_LIKE}|no\s+more\s+{_LIKE}"), {"action": "less"}),
    (
        _cmd(rf"not\s+{_THIS}|i\s+don'?t\s+like\s+{_THIS}"),
        {"action": "less"},
    ),
    (_cmd(rf"more\s+{_LIKE}"), {"action": "more"}),
]
_VOLUME = re.compile(
    rf"^\s*{POLITE}(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+)?(?P<n>\d{{1,3}})\s*%?{TAIL}",
    re.IGNORECASE,
)


def music_fast_path(message: str) -> tuple[str, dict] | None:
    for pattern, params in _DJ:
        if pattern.match(message):
            return "dj_mode", dict(params)
    m = _VOLUME.match(message)
    if m:
        return "media_control", {"action": "volume", "level": int(m["n"])}
    for pattern, skill, params in _COMMANDS:
        if pattern.match(message):
            return skill, dict(params)
    m = _PLAY.match(message)
    if m and not _NOT_MUSIC.match(m["query"]):
        return "play_music", {"query": m["query"].strip()}
    return None
