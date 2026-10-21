"""Her feature switches (Mario 2026-10-21: "do a full feature system, this way
i can actually know what she does and what she doesn't"). The list with what
each one costs is runbooks/felt-features.md in the workspace repo; the keys
here are the same.

A cache probe that day showed unprompted background lines holding felt-llm's
one slot ~80 % of the time, so chat waited behind them. Each feature checks
its switch before it spends a model call; a switch takes effect on the next
call (settings table, no restart). Off by default: the three GPT's review
named as pure decoration until there is a scheduler - asides, marks and the
pre-written weather line.

Skills are switches too (`skill.<name>`, on unless turned off): off takes the
tool out of her list, for chat and the warm-up alike, so the cached prefix
stays the one chat uses.
"""

from felt_core.db import get_setting, set_setting

FEATURES: dict[str, tuple[bool, str]] = {
    "ambient.screen_reactions": (True, "Comments on your screen (screenshot), ~90 s cooldown"),
    "ambient.poe_reactions": (True, "PoE 2: prices the loot on your screen"),
    "ambient.desktop_events": (True, "Lines about game start/stop, back from AFK, GPU hot, calls"),
    "ambient.song_remarks": (True, "A remark when a new song starts"),
    "ambient.dj_lines": (True, "DJ mode: says a line as each pick comes on (picking stays)"),
    "ambient.asides": (False, "App Talk page: re-picks her asides after every remark"),
    "ambient.marks": (False, "App notebook: decides on a circle/underline after each reply"),
    "ambient.weather_line": (False, "Re-writes a ready weather line every 15 min"),
    "ambient.morning_greeting": (True, "The diary's morning line"),
    "email.new_mail": (True, "'Mail from X, want a summary?' when mail arrives"),
    "calendar.headsup": (True, "Heads-up 30 and 10 min before events, leave-now lines"),
    "ender.print_done": (True, "Ender print finished/failed lines"),
    "memory.diary": (True, "Nightly diary + self-memories"),
}

SKILL_PREFIX = "skill."


def _key(name: str) -> str:
    return f"feature:{name}"


def known(name: str) -> bool:
    return name in FEATURES or name.startswith(SKILL_PREFIX)


def enabled(db_path: str, name: str) -> bool:
    if not known(name):
        raise ValueError(f"unknown feature {name!r}")
    default = FEATURES[name][0] if name in FEATURES else True
    value = get_setting(db_path, _key(name))
    return default if value is None else value == "1"


def set_enabled(db_path: str, name: str, on: bool) -> None:
    if not known(name):
        raise ValueError(f"unknown feature {name!r}")
    set_setting(db_path, _key(name), "1" if on else "0")


def enabled_skills(db_path: str, registry: dict) -> dict:
    """The skill registry without the switched-off skills."""
    if not registry:
        return registry
    return {n: s for n, s in registry.items() if enabled(db_path, SKILL_PREFIX + n)}


def listing(db_path: str, skill_names: list[str]) -> list[dict]:
    rows = [
        {"key": k, "on": enabled(db_path, k), "default": d, "what": what}
        for k, (d, what) in FEATURES.items()
    ]
    rows += [
        {"key": SKILL_PREFIX + n, "on": enabled(db_path, SKILL_PREFIX + n), "default": True}
        for n in sorted(skill_names)
    ]
    return rows
