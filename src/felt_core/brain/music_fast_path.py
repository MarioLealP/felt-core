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
# "it's way too loud", "ugh the music is too quiet"
_TOO = (
    r"(?:ugh,?\s+)?(?:(?:it|that|this|the\s+(?:music|song))(?:'s|\s+is)\s+)?"
    r"(?:way\s+|a\s+bit\s+|kinda\s+|so\s+)?too\s+"
)


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
            rf"|hold\s+{_NAMED}(?:\s+(?:a\s+sec(?:ond)?|for\s+a\s+(?:sec(?:ond)?|bit|moment)))?"
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
        _cmd(
            rf"(?:turn\s+{_MUSIC}\s+up|louder|volume\s+up|turn\s+up\s+{_MUSIC}"
            rf"|{_TOO}(?:quiet|soft|low)|make\s+{_MUSIC}\s+(?:a\s+(?:bit|little)\s+)?louder)"
        ),
        "media_control",
        {"action": "louder"},
    ),
    (
        _cmd(
            rf"(?:turn\s+{_MUSIC}\s+down|quieter|softer|volume\s+down|turn\s+down\s+{_MUSIC}"
            rf"|{_TOO}loud|make\s+{_MUSIC}\s+(?:a\s+(?:bit|little)\s+)?(?:quieter|softer))"
        ),
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
_PLAY = re.compile(
    rf"^\s*{POLITE}(?:play|(?P<put>put|throw|pop)\s+on)\s+(?:me\s+)?(?P<query>.{{2,80}}?){TAIL}",
    re.IGNORECASE,
)
# "queue birdbrain", "add birdbrain to the queue", "play birdbrain next" -
# queued only, not played now (Mario 2026-10-18); "add" needs the queue named
# ("add milk to the list" is the shopping list)
_INTO_QUEUE = r"(?:to|in|on|into)\s+(?:the|my)\s+queue"
_QUEUE = re.compile(
    rf"^\s*{POLITE}(?:queue(?:\s+up)?\s+(?P<q1>.{{2,80}}?)(?:\s+{_INTO_QUEUE})?"
    rf"|(?:add|put|stick|throw)\s+(?P<q2>.{{2,80}}?)\s+{_INTO_QUEUE}"
    rf"|play\s+(?P<q3>.{{2,80}}?)\s+next)(?:\s+for\s+later)?{TAIL}",
    re.IGNORECASE,
)
# "put on your hoodie" is an outfit (she has them), not a song
_NOT_WORN = re.compile(
    r"^(?:your|my|her|the|a|an)\b|\b(?:clothes|outfit|pajamas|pyjamas|hoodie|jacket|coat|hat)\b",
    re.IGNORECASE,
)
_NOT_MUSIC = re.compile(
    r"^(?:a\s+)?(?:games?\b|with\b|along\b|nice\b|around\b|dead\b|chess\b|tag\b"
    r"|hide\b|pretend\b|fair\b|it\s+(?:cool|safe|by\s+ear)\b|on\b|for\b)",
    re.IGNORECASE,
)
_THIS = r"(?:this|that)(?:\s+(?:one|song|track))?"
# "more like this", "less of that", "fewer songs like this one"
# l+ike: "less llike this" (Mario, 2026-10-18 - typed fast, it paused)
_LIKE = rf"(?:(?:songs?|stuff|tracks?)\s+)?(?:l+i+k+e+|of)\s+{_THIS}"
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
    (_cmd(rf"(?:no\s+)?(?:l+e+s+|fewer)\s+{_LIKE}|no\s+more\s+{_LIKE}"), {"action": "less"}),
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


# "ugh this song sucks, next" - a skip tacked onto a complaint
_TRAILING_SKIP = re.compile(
    r"[,.!]\s*(?:next|skip(?:\s+(?:it|this|this\s+one))?)\s*[.!?]*$", re.IGNORECASE
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
    if _TRAILING_SKIP.search(message):
        return "media_control", {"action": "next"}
    m = _QUEUE.match(message)
    if m:
        query = m["q1"] or m["q2"] or m["q3"]
        return "play_music", {"query": query.strip(), "when": "queue"}
    m = _PLAY.match(message)
    if m and not _NOT_MUSIC.match(m["query"]) and not (m["put"] and _NOT_WORN.search(m["query"])):
        return "play_music", {"query": m["query"].strip()}
    return None
