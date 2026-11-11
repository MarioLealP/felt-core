"""Her feature switches.

Her feature switches (Mario 2026-10-21: "do a full feature system, this way
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

from felt_core.db.settings import get_setting, set_setting, settings_with_prefix

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
    "email.new_mail": (True, "'Btw, mail from X' - at most hourly, list mail judged by her"),
    "calendar.headsup": (True, "Heads-up 30 and 10 min before events, leave-now lines"),
    "ender.print_done": (True, "Ender print finished/failed lines"),
    "ha.tell": (True, "Anything an HA automation asks her to tell you (felt.tell)"),
    "brief.morning": (True, "Morning brief: today, weather, news, overnight alerts"),
    "memory.diary": (True, "Nightly diary + self-memories"),
}

SKILL_PREFIX = "skill."


def _key(name: str) -> str:
    return f"feature:{name}"


def known(name: str) -> bool:
    """Whether `name` is a feature or a skill switch."""
    return name in FEATURES or name.startswith(SKILL_PREFIX)


def _is_on(name: str, value: str | None) -> bool:
    if not known(name):
        raise ValueError(f"unknown feature {name!r}")
    default = FEATURES[name][0] if name in FEATURES else True
    return default if value is None else value == "1"


async def enabled(db_path: str, name: str) -> bool:
    """Whether a feature is on (its default until Mario sets it)."""
    return _is_on(name, await get_setting(db_path, _key(name)))


async def set_enabled(db_path: str, name: str, on: bool) -> None:
    """Switch a feature on or off."""
    if not known(name):
        raise ValueError(f"unknown feature {name!r}")
    await set_setting(db_path, _key(name), "1" if on else "0")


async def _switches(db_path: str) -> dict[str, str]:
    """Every stored switch, keyed by feature / skill name - one query instead of one per switch."""
    stored = await settings_with_prefix(db_path, _key(""))
    return {key.removeprefix(_key("")): value for key, value in stored.items()}


async def enabled_skills(db_path: str, registry: dict) -> dict:
    """The skill registry without the switched-off skills."""
    if not registry:
        return registry
    switches = await _switches(db_path)
    return {n: s for n, s in registry.items() if _is_on(SKILL_PREFIX + n, switches.get(SKILL_PREFIX + n))}


async def listing(db_path: str, skill_names: list[str]) -> list[dict]:
    """Every feature and skill switch with its state, default and description."""
    switches = await _switches(db_path)
    rows = [
        {"key": k, "on": _is_on(k, switches.get(k)), "default": d, "what": what} for k, (d, what) in FEATURES.items()
    ]
    rows += [
        {"key": SKILL_PREFIX + n, "on": _is_on(SKILL_PREFIX + n, switches.get(SKILL_PREFIX + n)), "default": True}
        for n in sorted(skill_names)
    ]
    return rows
