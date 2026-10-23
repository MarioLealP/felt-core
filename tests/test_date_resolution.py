from datetime import date, datetime

import pytest

from felt_core.date_resolution import (
    day_only,
    fix_typos,
    has_clock,
    resolve_date,
    resolve_datetime,
    upcoming_weekend,
)

# A fixed Wednesday, so weekday-relative phrases are unambiguous.
_TODAY = date(2026, 9, 16)
_NOW = datetime(2026, 9, 16, 22, 0, 0)  # 10pm, so "already passed today" cases are exercised


def test_resolve_date_today():
    assert resolve_date("today", _TODAY) == _TODAY


def test_resolve_date_tomorrow():
    assert resolve_date("tomorrow", _TODAY) == date(2026, 9, 17)


def test_resolve_date_in_n_days():
    assert resolve_date("in 3 days", _TODAY) == date(2026, 9, 19)


def test_resolve_date_next_weekday_prefers_future():
    # "tuesday" from a Wednesday should mean *next* Tuesday, not
    # yesterday — PREFER_DATES_FROM=future is what guarantees this.
    result = resolve_date("tuesday", _TODAY)
    assert result is not None
    assert result > _TODAY
    assert result.weekday() == 1  # Tuesday


def test_resolve_date_next_weekday_with_next_prefix():
    # Real dateparser quirk found 2026-09-12: "next tuesday" parses to
    # None outright (even bare "tuesday" works) — needs the fallback
    # that strips the leading "next" before retrying.
    result = resolve_date("next tuesday", _TODAY)
    assert result is not None
    assert result > _TODAY
    assert result.weekday() == 1  # Tuesday


def test_resolve_date_explicit_iso_date():
    assert resolve_date("2026-09-25", _TODAY) == date(2026, 9, 25)


def test_resolve_date_unparseable_returns_none():
    assert resolve_date("asdkjfh not a date", _TODAY) is None


def test_upcoming_weekend_from_midweek():
    saturday, sunday = upcoming_weekend(_TODAY)  # Wednesday 2026-09-16
    assert saturday == date(2026, 9, 19)
    assert sunday == date(2026, 9, 20)


def test_upcoming_weekend_from_saturday_is_same_day():
    saturday_today = date(2026, 9, 19)
    saturday, sunday = upcoming_weekend(saturday_today)
    assert saturday == saturday_today
    assert sunday == date(2026, 9, 20)


def test_upcoming_weekend_from_sunday_is_next_weekend():
    sunday_today = date(2026, 9, 20)
    saturday, sunday = upcoming_weekend(sunday_today)
    assert saturday == date(2026, 9, 26)
    assert sunday == date(2026, 9, 27)


def test_resolve_datetime_already_passed_time_rolls_to_tomorrow():
    # Asked at 10pm for 7:30am — must mean tomorrow morning, not the
    # 7:30am that already happened earlier today.
    result = resolve_datetime("7:30am", _NOW)
    assert result == datetime(2026, 9, 17, 7, 30, 0)


def test_resolve_datetime_still_upcoming_time_today():
    result = resolve_datetime("11pm", _NOW)
    assert result == datetime(2026, 9, 16, 23, 0, 0)


def test_resolve_datetime_explicit_day_and_time():
    result = resolve_datetime("tomorrow at 9am", _NOW)
    assert result == datetime(2026, 9, 17, 9, 0, 0)


def test_resolve_datetime_unparseable_returns_none():
    assert resolve_datetime("asdkjfh not a time", _NOW) is None


# --- bare hours + lone days (2026-10-18: "monday at 9" was September 2027) ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("monday at 9", datetime(2026, 10, 19, 9, 0)),
        ("tomorrow at 9", datetime(2026, 10, 19, 9, 0)),
        ("friday at 3", datetime(2026, 10, 23, 15, 0)),  # 1-7 = afternoon
        ("at 9 o'clock", datetime(2026, 10, 19, 9, 0)),
        ("friday at 3pm", datetime(2026, 10, 23, 15, 0)),
        ("at 7:30", datetime(2026, 10, 19, 7, 30)),
        ("the 1st at 9", datetime(2026, 11, 1, 9, 0)),
        ("on the 31st at 9am", datetime(2026, 10, 31, 9, 0)),
        ("the 1st of december at 9", datetime(2026, 12, 1, 9, 0)),
    ],
)
def test_bare_hours_and_lone_days(text, expected):
    assert resolve_datetime(text, datetime(2026, 10, 18, 15, 20)) == expected


def test_an_alarm_bare_hour_is_morning():
    now = datetime(2026, 10, 18, 22, 0)
    assert resolve_datetime("at 6", now, pm_below=0) == datetime(2026, 10, 19, 6, 0)


# --- typos, a stale year, day-first numeric dates (2026-10-22, cm-0vl) ---

NOW = datetime(2026, 10, 22, 11, 14)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("on4th of novemeber", datetime(2026, 11, 4)),
        ("novemeber 4th at 9:00", datetime(2026, 11, 4, 9, 0)),
        ("2025-11-04 at 9:00", datetime(2026, 11, 4, 9, 0)),  # felt-llm's year lags
        ("November 4, 2025 at 9:00", datetime(2026, 11, 4, 9, 0)),
        ("04/11/2026 at 9:00", datetime(2026, 11, 4, 9, 0)),  # day first
        ("4/11 at 9:00", datetime(2026, 11, 4, 9, 0)),
        ("25.12 at 9:00", datetime(2026, 12, 25, 9, 0)),
        ("2026-11-04T09:00", datetime(2026, 11, 4, 9, 0)),  # ISO stays ISO
        ("2026-11-04", datetime(2026, 11, 4)),
        ("12/25 at 9:00", None),  # US order is no date at all (Mario)
    ],
)
def test_typos_stale_years_and_day_first(text, expected):
    assert resolve_datetime(text, NOW) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4 decmeber", "4 december"),
        ("the 4th of novemeber", "the 4th of november"),
        ("remember 4 things", "remember 4 things"),  # close to december, but a word
        ("the member meeting at 9", "the member meeting at 9"),
    ],
)
def test_fix_typos(text, expected):
    assert fix_typos(text) == expected


@pytest.mark.parametrize(
    ("text", "clock"),
    [("4th november", False), ("tomorrow", False), ("friday at 3", True), ("3pm", True)],
)
def test_has_clock(text, clock):
    assert has_clock(text) is clock


@pytest.mark.parametrize(
    ("when", "message", "expected"),
    [
        # live 2026-10-23: felt-llm added 3pm Mario never said
        ("4 November 15:00", "put the 4th of November in my calendar as WoW Forever release", True),
        ("nov 4 9am", "Add reminder for wow forever release on4th of novemeber", True),
        ("4th november", "", True),
        ("tomorrow at 11am", "schedule my dentist to tomorrow at 11", False),
        # follow-ups name no day: her `when` decides
        ("tomorrow at 11am", "none", False),
        ("tomorrow at 11am", "it's at Clínica Sorriso in Gaia", False),
        ("friday 3pm", "decide on the dentist friday at 3", False),
    ],
)
def test_day_only(when, message, expected):
    assert day_only(when, message) is expected
