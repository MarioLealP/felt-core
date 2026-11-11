"""The prompt for her line about a desktop event: whitelisted fields only, worded per event type."""

from __future__ import annotations

_MAX_FIELD_CHARS = 120

_EVENT_INSTRUCTION = (
    "Say ONE short, natural spoken remark about it (max ~15 words), in character. "
    "React like a friend in the room. No questions that need an answer."
)


def _text(details: dict, key: str) -> str:
    value = details.get(key)
    if value is None:
        return ""
    return " ".join(str(value).split())[:_MAX_FIELD_CHARS]


def _number(details: dict, key: str) -> float | None:
    try:
        return float(details[key])
    except KeyError, TypeError, ValueError:
        return None


def _game_name(details: dict) -> str:
    name = _text(details, "name") or _text(details, "game")
    return name[:-4] if name.lower().endswith(".exe") else name


# WP-30: felt-animation's hobby keys -> how she'd say them. Whitelisted like
# every other detail here - an unknown key is simply not mentioned.
HOBBY_PHRASES = {
    "reading": "reading a book",
    "gaming": "playing on your handheld",
    "napping": "napping",
    "tidying": "tidying up your stuff",
    "stretching": "stretching and looking around",
}


def _hobbies_phrase(details: dict) -> str:
    """The hobby line: "reading a book (about 12 min) and napping" from details["hobbies"]."""
    parts = []
    for h in details.get("hobbies") or []:
        if not isinstance(h, dict) or h.get("hobby") not in HOBBY_PHRASES:
            continue
        mins = _number(h, "minutes")
        parts.append(HOBBY_PHRASES[h["hobby"]] + (f" (about {round(mins)} min)" if mins else ""))
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0] if parts else ""


def _spoken_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"about {max(minutes, 1)} minutes"
    hours = round(seconds / 3600 * 2) / 2
    return f"about {hours:g} hours"


UNHUSHED_MAX_WORDS = 12


def _unhushed_prompt(details: dict, previous: str | None) -> str:
    hushed_for = _number(details, "hushed_for_s") or 0
    planned = _number(details, "planned_s") or 0
    early = details.get("early") is True
    if early and planned:
        lasted = "a few seconds" if hushed_for < 60 else _spoken_duration(hushed_for)
        situation = (
            f"Mario hushed you for {_spoken_duration(planned)} but let you talk again after only "
            f"{lasted}. Tease him that he couldn't last without you."
        )
    elif planned:
        situation = (
            f"Mario hushed you and you stayed quiet the whole {_spoken_duration(planned)}. "
            "Be dramatic about how hard it was to stay quiet that long."
        )
    else:
        situation = "Mario just let you talk again after hushing you. Tease him about it."
    avoid = f' Your last line when unhushed was "{previous}" - say something different.' if previous else ""
    return (
        f"{situation}{avoid} Say ONE short, snappy, teasing line (max {UNHUSHED_MAX_WORDS} words), "
        "in character. No questions that need an answer. Plain spoken words only: no *actions*, "
        "stage directions, sound words like *yawn*, or emoji."
    )


def build_prompt(event: str, details: dict, lyric: str | None = None, previous: str | None = None) -> str | None:
    """Pure: the user-turn text for an event, from whitelisted fields only.

    None if the details are too thin to say anything real about. `previous` is this event's last spoken line (unhushed
    uses it so the same line never plays twice in a row).
    """
    if event == "unhushed":
        return _unhushed_prompt(details, previous)
    if event == "music_new_track":
        title, artist, album = (
            _text(details, "title"),
            _text(details, "artist"),
            _text(details, "album"),
        )
        if not title:
            return None
        song = f'"{title}"' + (f" by {artist}" if artist else "") + (f" (from {album})" if album else "")
        lyric_part = f' A line from it: "{lyric}". You may quote it if it fits.' if lyric else ""
        return f"Mario just started playing {song}.{lyric_part} {_EVENT_INSTRUCTION}"
    if event == "dj_track":
        title, artist = _text(details, "title"), _text(details, "artist")
        if not title:
            return None
        song = f'"{title}"' + (f" by {artist}" if artist else "")
        because = _text(details, "because")
        why = (
            "It's an artist new to him - you're putting him onto it, so sell it a little"
            + (f" (you picked it because: {because})." if because else ".")
            if details.get("new") is True
            else "It's one he already likes."
        )
        return (
            f"You're DJ-ing Mario's music. Your pick is coming on now: {song}. {why} "
            f"Introduce it like a radio DJ, in character. {_EVENT_INSTRUCTION}"
        )
    if event == "game_start":
        name = _game_name(details)
        what = f"the game {name}" if name else "a game"
        return f"Mario just launched {what}. Get comfy with him. {_EVENT_INSTRUCTION}"
    if event == "game_stop":
        name = _game_name(details)
        what = f"{name}" if name else "his game"
        duration = _number(details, "duration_s")
        took = f" after {_spoken_duration(duration)}" if duration else ""
        return f"Mario just closed {what}{took}. Something like 'finally done?'. {_EVENT_INSTRUCTION}"
    if event == "afk_end":
        duration = _number(details, "duration_s") or 0
        hobbies = _hobbies_phrase(details)
        pastime = (
            f" While he was gone you spent the time {hobbies}; you can mention it in passing "
            "with a small opinion (like 'finished two chapters, it was mid'), only if it fits."
            if hobbies
            else ""
        )
        return (
            f"Mario just came back to his desk after being away for {_spoken_duration(duration)}. "
            f"Greet him.{pastime} {_EVENT_INSTRUCTION}"
        )
    if event == "gpu_hot":
        temp = _number(details, "temp_c")
        heat = f" - the GPU is at {round(temp)} degrees" if temp else ""
        return f"Mario's PC is running hot{heat}. You grab your water bottle to cool off. {_EVENT_INSTRUCTION}"
    if event == "call_join_guess":
        return f"Someone seems to have just joined Mario's voice call. A small, quiet hello. {_EVENT_INSTRUCTION}"
    if event == "poe_item":
        return _poe_item_prompt(details)
    return None


def _poe_item_prompt(details: dict) -> str | None:
    """Mario price-checked an item.

    Details filled by react_to_event from the item text: name, base, rarity, prices. Only with a real price - she never
    invents one; rares stay Exiled Exchange's job for now (WP-57).
    """
    name = _text(details, "name")
    if not name:
        return None
    rarity = _text(details, "rarity").lower()
    base = _text(details, "base")
    what = f"{name}" + (f" ({rarity} {base})" if base and base != name else f" ({rarity})" if rarity else "")
    prices = [str(p) for p in details.get("prices") or [] if str(p).strip()][:2]
    if prices:
        return (
            f"Mario just price-checked {what} in Path of Exile 2. Live market: {' | '.join(prices)}. "
            "Say ONE short spoken line (max ~20 words), in character, that names the item and its "
            "price the way a person says it out loud (round it: 'like forty divines', 'a couple of "
            "exalts'). Big value: loot-goblin hype. Worthless: tease him. No lists, no markdown."
        )
    return None  # no price: she stays quiet (react_to_event skips "unpriced" first)
