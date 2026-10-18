"""Arbitrary day/date-phrase resolution, extracted so it can be unit
tested without a network call. Built to replace get_weather's original
fixed now/today/tomorrow/weekend string matching (flagged by Mario as
"not right" on 2026-09-12) with genuine parsing of whatever phrase the
router happens to pass through — "next tuesday", "in 3 days", a literal
date, etc.
"""

import re
from datetime import date, datetime, timedelta

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


def resolve_datetime(text: str, now: datetime, pm_below: int = 8) -> datetime | None:
    """Parse an arbitrary time/date phrase ("7:30am", "9pm", "tomorrow at
    9am") into a concrete future datetime relative to `now`. Used by
    set_alarm, where (unlike get_weather's date-only resolve_date above)
    the time of day itself is the whole point. Confirmed 2026-09-12:
    dateparser's PREFER_DATES_FROM="future" already rolls a bare
    already-passed time (e.g. "7:30am" asked at 10pm) to tomorrow on its
    own, so this needs no extra fallback logic of its own. A bare "at 9"
    goes through clock_bare_hours first.
    """
    return dateparser.parse(
        name_the_month(clock_bare_hours(text, pm_below), now),
        settings={"RELATIVE_BASE": now, "PREFER_DATES_FROM": "future"},
    )


def upcoming_weekend(today: date) -> tuple[date, date]:
    """The next Saturday+Sunday pair from `today` — if `today` already
    falls on the weekend, that same weekend is returned rather than the
    following one.
    """
    days_until_saturday = (5 - today.weekday()) % 7
    saturday = today + timedelta(days=days_until_saturday)
    return saturday, saturday + timedelta(days=1)
