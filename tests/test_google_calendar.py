from datetime import date

from felt_core.google_calendar import (
    TZ,
    day_bounds,
    events_url,
    format_event_line,
    resolve_calendar,
    summarize_events,
)


def test_events_url_encodes_special_characters_in_calendar_id():
    # 2026-09-14, caught live: the public-holidays calendar's real ID is
    # "pt.portuguese#holiday@group.v.calendar.google.com" — an
    # unencoded "#" got parsed as a URL fragment separator, silently
    # truncating the actual request path.
    url = events_url("pt.portuguese#holiday@group.v.calendar.google.com")
    assert "#" not in url
    assert url == (
        "https://www.googleapis.com/calendar/v3/calendars/"
        "pt.portuguese%23holiday%40group.v.calendar.google.com/events"
    )


def test_day_bounds_is_a_full_local_day():
    start, end = day_bounds(date(2026, 9, 18))
    assert start.isoformat() == "2026-09-18T00:00:00+01:00"
    assert end.isoformat() == "2026-09-19T00:00:00+01:00"
    assert start.tzinfo == TZ


def test_resolve_calendar_no_name_returns_primary():
    calendars = [
        {"id": "a", "summary": "Public Holidays", "primary": False},
        {"id": "b", "summary": "Mario", "primary": True},
    ]
    assert resolve_calendar(calendars, None)["id"] == "b"


def test_resolve_calendar_exact_match_preferred_over_partial():
    calendars = [
        {"id": "a", "summary": "Professional"},
        {"id": "b", "summary": "Professional Extras"},
    ]
    assert resolve_calendar(calendars, "Professional")["id"] == "a"


def test_resolve_calendar_partial_match():
    calendars = [{"id": "a", "summary": "Public Holidays"}]
    assert resolve_calendar(calendars, "holidays")["id"] == "a"


def test_resolve_calendar_no_match_returns_none():
    calendars = [{"id": "a", "summary": "Public Holidays"}]
    assert resolve_calendar(calendars, "birthdays") is None


def test_format_event_line_timed_event():
    event = {"summary": "Dentist", "start": {"dateTime": "2026-09-18T15:00:00+01:00"}}
    assert format_event_line(event) == "Dentist at 15:00"


def test_format_event_line_all_day_event():
    event = {"summary": "Freedom Day", "start": {"date": "2026-04-25"}}
    assert format_event_line(event) == "Freedom Day (all day)"


def test_summarize_events_empty():
    assert summarize_events([]) == "Nothing on your calendar."


def test_summarize_events_drops_empty_calendars_and_includes_source():
    events_by_calendar = [
        ("Mario", [{"summary": "Standup", "start": {"dateTime": "2026-09-18T09:00:00+01:00"}}]),
        ("Public Holidays", []),
    ]
    assert summarize_events(events_by_calendar) == "You have: Standup at 09:00 (Mario)."
