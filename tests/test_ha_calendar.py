"""ha_calendar: Mario's calendars through Home Assistant (2026-10-19)."""

import json
from datetime import date, datetime

import httpx

from felt_core.config import FeltConfig
from felt_core.ha_calendar import (
    TZ,
    day_bounds,
    format_event_line,
    get_event,
    link,
    list_calendars,
    list_events,
    resolve_calendar,
    summarize_events,
    writable,
)

CONFIG = FeltConfig(ha_url="http://ha", ha_token="t")


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_calendars_mark_the_default_as_primary():
    def handler(request):
        assert request.url.path == "/api/calendars"
        assert request.headers["authorization"] == "Bearer t"
        return httpx.Response(
            200,
            json=[
                {"entity_id": "calendar.feriados_em_portugal", "name": "Feriados"},
                {"entity_id": "calendar.lealmario47_gmail_com", "name": "Mario"},
            ],
        )

    async with _client(handler) as client:
        cals = await list_calendars(client, CONFIG)
    assert [(c["id"], c["primary"]) for c in cals] == [
        ("calendar.feriados_em_portugal", False),
        ("calendar.lealmario47_gmail_com", True),
    ]
    primary = resolve_calendar(cals, None)
    assert primary is not None and primary["summary"] == "Mario"


async def test_events_get_an_id_per_occurrence():
    def handler(request):
        assert request.url.params["start"] == "2026-10-20T00:00:00+01:00"
        return httpx.Response(
            200,
            json=[
                {"uid": "once", "recurrence_id": None, "summary": "TAC"},
                {"uid": "bins", "recurrence_id": "bins_20261020T080000Z", "summary": "bins"},
            ],
        )

    start, end = day_bounds(date(2026, 10, 20))
    async with _client(handler) as client:
        events = await list_events(client, CONFIG, "calendar.x", start, end)
    assert [e["id"] for e in events] == ["once", "bins_20261020T080000Z"]


async def test_get_event_refreshes_first_and_finds_it_by_id():
    seen = []

    def handler(request):
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            assert json.loads(request.content) == {"entity_id": "calendar.x"}
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"uid": "a", "summary": "A"}])

    around = datetime(2026, 10, 20, 16, 30, tzinfo=TZ)
    async with _client(handler) as client:
        found = await get_event(client, CONFIG, "calendar.x", "a", around)
        assert found is not None and found["summary"] == "A"
        assert await get_event(client, CONFIG, "calendar.x", "gone", around) is None
    assert seen[:2] == [
        "POST /api/services/homeassistant/update_entity",
        "GET /api/calendars/calendar.x",
    ]


async def test_writable_reads_the_create_feature():
    def handler(request):
        features = 3 if request.url.path.endswith("mario") else None
        return httpx.Response(200, json={"attributes": {"supported_features": features}})

    async with _client(handler) as client:
        assert await writable(client, CONFIG, "calendar.mario")
        assert not await writable(client, CONFIG, "calendar.feriados")


def test_link_is_the_first_web_link_in_place_or_description():
    assert link({"location": "https://zoom.us/j/1", "description": "x"}) == "https://zoom.us/j/1"
    assert link({"description": "join (https://meet.google.com/abc)."}) == (
        "https://meet.google.com/abc"
    )
    assert link({"location": "Hospital da Luz", "description": None}) is None


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
    result = resolve_calendar(calendars, None)
    assert result is not None
    assert result["id"] == "b"


def test_resolve_calendar_exact_match_preferred_over_partial():
    calendars = [
        {"id": "a", "summary": "Professional"},
        {"id": "b", "summary": "Professional Extras"},
    ]
    result = resolve_calendar(calendars, "Professional")
    assert result is not None
    assert result["id"] == "a"


def test_resolve_calendar_partial_match():
    calendars = [{"id": "a", "summary": "Public Holidays"}]
    result = resolve_calendar(calendars, "holidays")
    assert result is not None
    assert result["id"] == "a"


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
