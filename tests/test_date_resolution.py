from datetime import date, datetime

from felt_core.date_resolution import resolve_date, resolve_datetime, upcoming_weekend

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
