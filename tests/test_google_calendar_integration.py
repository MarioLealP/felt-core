"""Real network calls, recorded once via VCR (see tests/conftest.py) —
the token-refresh + read paths (list_calendars, list_events) against
the real Google Calendar API. create_event is deliberately NOT covered
here: it's a real write against Mario's actual calendar, so it's
live-verified once by hand (create, confirm, delete) rather than
committed as a replayable cassette that would otherwise require a
throwaway event to exist forever — same "don't fake a live-I/O test"
principle as the rest of this project's genuinely-untested live paths,
just applied to a write instead of a poll loop."""

from datetime import date

import pytest

from felt_core.google_calendar import day_bounds, list_calendars, list_events


@pytest.mark.vcr
async def test_list_calendars_returns_real_calendars(google_calendar_config):
    import httpx

    async with httpx.AsyncClient(timeout=20) as client:
        calendars = await list_calendars(client, google_calendar_config)

    assert any(c.get("primary") for c in calendars)
    assert len(calendars) >= 1


@pytest.mark.vcr
async def test_list_events_for_a_real_day(google_calendar_config):
    import httpx

    start, end = day_bounds(date(2026, 9, 15))
    async with httpx.AsyncClient(timeout=20) as client:
        calendars = await list_calendars(client, google_calendar_config)
        primary = next(c for c in calendars if c.get("primary"))
        events = await list_events(client, google_calendar_config, primary["id"], start, end)

    assert isinstance(events, list)
