"""Arbitrary day/date-phrase resolution, extracted so it can be unit
tested without a network call. Built to replace get_weather's original
fixed now/today/tomorrow/weekend string matching (flagged by Mario as
"not right" on 2026-09-12) with genuine parsing of whatever phrase the
router happens to pass through — "next tuesday", "in 3 days", a literal
date, etc.
"""

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


def upcoming_weekend(today: date) -> tuple[date, date]:
    """The next Saturday+Sunday pair from `today` — if `today` already
    falls on the weekend, that same weekend is returned rather than the
    following one.
    """
    days_until_saturday = (5 - today.weekday()) % 7
    saturday = today + timedelta(days=days_until_saturday)
    return saturday, saturday + timedelta(days=1)
