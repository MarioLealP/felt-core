"""Arbitrary day/date-phrase resolution, extracted so it can be unit
tested without a network call. Built to replace get_weather's original
fixed now/today/tomorrow/weekend string matching (flagged by Mario as
"not right" on 2026-09-12) with genuine parsing of whatever phrase the
router happens to pass through — "next tuesday", "in 3 days", a literal
date, etc.
"""

import re
from datetime import date, datetime, timedelta
from difflib import get_close_matches

import dateparser

_WEEKDAY_NAMES = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def resolve_date(day_text: str, today: date) -> date | None:
    """Parse an arbitrary day/date phrase relative to `today`. Returns
    None if `day_text` isn't a recognizable date reference at all.
    """
    parsed = _parse(day_text, today)
    if parsed is not None:
        return parsed

    # dateparser quirk (verified 2026-09-12): "next tuesday" parses to
    # None even with PREFER_DATES_FROM=future, while bare "tuesday" (or
    # "on tuesday") resolves correctly to the next occurrence. "next
    # <weekday>" is common enough phrasing to be worth a fallback: strip
    # the leading "next" and retry on the bare weekday name.
    words = day_text.strip().lower().split()
    if len(words) == 2 and words[0] == "next" and words[1] in _WEEKDAY_NAMES:
        return _parse(words[1], today)

    return None


def _parse(day_text: str, today: date) -> date | None:
    parsed = dateparser.parse(
        day_text,
        settings={
            "RELATIVE_BASE": datetime.combine(today, datetime.min.time()),
            "PREFER_DATES_FROM": "future",
        },
    )
    return parsed.date() if parsed else None


# "at 9" / "at 3" with no minutes or am/pm: dateparser reads the number as
# a month ("monday at 9" -> September 2027, "friday at 3" -> March) or
# drops it ("tomorrow at 9" -> tomorrow at the current time) - 2026-10-18
_BARE_HOUR = re.compile(
    r"\bat\s+(\d{1,2})(?:\s*o'?clock)?\b(?!\s*(?::|\.\d|h\b|am\b|pm\b|a\.m|p\.m))",
    re.IGNORECASE,
)


def clock_bare_hours(text: str, pm_below: int = 8) -> str:
    """Pure: 'friday at 3' -> 'friday at 15:00', 'monday at 9' -> 'monday at
    9:00'. Hours under `pm_below` count as afternoon (what people mean by
    'at 3'); alarms pass 0 - 'wake me at 6' is morning."""

    def clock(m: re.Match) -> str:
        hour = int(m[1])
        if hour > 23:
            return m[0]
        if 1 <= hour < pm_below:
            hour += 12
        return f"at {hour}:00"

    return _BARE_HOUR.sub(clock, text)


# "the 1st at 9" with no month: dateparser takes the 1 as January
_LONE_DAY = re.compile(
    r"\b(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b(?!\s+(?:of\s+)?[a-z]{3})"
)
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip


def name_the_month(text: str, now: datetime) -> str:
    """Pure: 'the 1st at 9:00' -> 'november 1st at 9:00' - the next month
    that has that day (this month if it's still ahead)."""
    m = _LONE_DAY.search(text)
    if m is None or any(month[:3] in text.lower() for month in _MONTHS):
        return text
    day = int(m[1])
    year, month = now.year, now.month
    for _ in range(13):
        try:
            if date(year, month, day) >= now.date():
                break
        except ValueError:
            pass  # no 31st in this month
        month, year = (1, year + 1) if month == 12 else (month + 1, year)
    return f"{text[: m.start()]}{_MONTHS[month - 1]} {day}{text[m.end() :]}"


# a time of day in the text ("at 9", "3pm", "15:30", "noon", "in 20 minutes")
_HAS_CLOCK = re.compile(
    r"\d\s*(?:am|pm|a\.m|p\.m)\b|\d:\d|\bat\s+\d|\b(?:noon|midnight|morning|evening|tonight)\b"
    r"|\bin\s+\d+\s*(?:mins?|minutes?|hours?|h)\b|\bin\s+an?\s+(?:hour|minute)",
    re.IGNORECASE,
)


def has_clock(text: str) -> bool:
    """Pure: does the phrase name a time of day, or only a day?"""
    return _HAS_CLOCK.search(text) is not None


# Mario types fast (2026-10-22 "on4th of novemeber"): dateparser gives up on a
# word run into its number and on a misspelt month
_GLUED = re.compile(r"(?<=[a-z]{2})(?=\d)", re.IGNORECASE)
_WORD = re.compile(r"[a-z]+|\d+(?:st|nd|rd|th)?", re.IGNORECASE)
# a numeric day and month ("4/11", "25.12", "4-11"), not inside an ISO date
_SLASHED = re.compile(r"(?<!\d[/.-])\b\d{1,2}[/.-]\d{1,2}\b")


def fix_typos(text: str) -> str:
    """Pure: 'on4th of novemeber' -> 'on 4th of november'. Only a word next to
    a number (past an "of") is taken for a month, so "the member meeting"
    stays as it is."""
    text = _GLUED.sub(" ", text)
    words = list(_WORD.finditer(text))
    for i, m in reversed(list(enumerate(words))):
        word = m[0].lower()
        if not word.isalpha() or len(word) < 4 or word in _MONTHS:
            continue
        near = [words[j][0] for j in (i - 1, i + 1) if 0 <= j < len(words)]
        if i >= 2 and words[i - 1][0].lower() == "of":
            near.append(words[i - 2][0])
        if not any(n[0].isdigit() for n in near):
            continue
        month = get_close_matches(word, _MONTHS, n=1, cutoff=0.8)
        if month:
            text = f"{text[: m.start()]}{month[0]}{text[m.end() :]}"
    return text


def _stale_year(text: str, parsed: datetime, now: datetime) -> datetime:
    """felt-llm's sense of the year lags (its training data): '4 November 2025'
    asked in October 2026 means this November, not a date already gone."""
    base = now.replace(tzinfo=None) if parsed.tzinfo is None else now
    years = [int(y) for y in re.findall(r"\b(20\d\d)\b", text)]
    if parsed >= base or not years or max(years) >= now.year:
        return parsed
    for year in (now.year, now.year + 1):
        try:
            moved = parsed.replace(year=year)
        except ValueError:
            continue  # 29 February
        if moved >= base:
            return moved
    return parsed


def resolve_datetime(text: str, now: datetime, pm_below: int = 8) -> datetime | None:
    """Parse an arbitrary time/date phrase ("7:30am", "9pm", "tomorrow at
    9am") into a concrete future datetime relative to `now`. Used by
    set_alarm, where (unlike get_weather's date-only resolve_date above)
    the time of day itself is the whole point. Confirmed 2026-09-12:
    dateparser's PREFER_DATES_FROM="future" already rolls a bare
    already-passed time (e.g. "7:30am" asked at 10pm) to tomorrow on its
    own, so this needs no extra fallback logic of its own. A bare "at 9"
    goes through clock_bare_hours first. A numeric date is day first, always
    ("4/11" = 4 November; Mario: US order "is worthy of dead") - "12/25" is no
    date at all, not Christmas; ISO dates stay as they are.
    """
    text = name_the_month(clock_bare_hours(fix_typos(text), pm_below), now)
    settings = {"RELATIVE_BASE": now, "PREFER_DATES_FROM": "future"}
    if _SLASHED.search(text):
        parsed = dateparser.parse(text, settings={**settings, "DATE_ORDER": "DMY"})
        if parsed is not None and parsed.year > now.year + 1:
            return None  # "12/25" read as day 12 of a 25th month -> a year far off
    else:
        parsed = dateparser.parse(text, settings=settings)
    return _stale_year(text, parsed, now) if parsed else None


def upcoming_weekend(today: date) -> tuple[date, date]:
    """The next Saturday+Sunday pair from `today` — if `today` already
    falls on the weekend, that same weekend is returned rather than the
    following one.
    """
    days_until_saturday = (5 - today.weekday()) % 7
    saturday = today + timedelta(days=days_until_saturday)
    return saturday, saturday + timedelta(days=1)
