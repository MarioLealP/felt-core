"""The desktop agenda route (2026-10-15): pure shaping + who may call it."""

from felt_core.calendar_api import agenda_items
from felt_core.machine_auth import authorize

_W = "spiffe://clockmost.internal/workload/"


def _ev(summary, start, end=None, **extra):
    return {"summary": summary, "start": start, "end": end or start, **extra}


def test_shapes_sorts_and_keeps_all_day_first_on_its_day():
    items = agenda_items(
        [
            (
                "Work",
                [
                    _ev("Standup", {"dateTime": "2026-10-16T09:30:00+01:00"}),
                    _ev("Holiday", {"date": "2026-10-16"}, {"date": "2026-10-17"}),
                ],
            ),
            (
                "Home",
                [_ev("Dentist", {"dateTime": "2026-10-15T15:00:00+01:00"}, location="Lisboa")],
            ),
        ]
    )
    assert [i["title"] for i in items] == ["Dentist", "Holiday", "Standup"]
    assert items[0]["calendar"] == "Home" and items[0]["location"] == "Lisboa"
    assert items[1]["all_day"] is True and items[2]["all_day"] is False


def test_drops_cancelled_and_keeps_only_minimal_fields():
    items = agenda_items(
        [
            (
                "Work",
                [
                    _ev("Gone", {"dateTime": "2026-10-16T10:00:00Z"}, status="cancelled"),
                    _ev(
                        "Call",
                        {"dateTime": "2026-10-16T12:00:00Z"},
                        description="secret notes, join https://meet.google.com/abc.",
                        attendees=[{"email": "x@y"}],
                    ),
                ],
            )
        ]
    )
    assert [i["title"] for i in items] == ["Call"]
    assert items[0]["link"] == "https://meet.google.com/abc"
    assert set(items[0]) == {"title", "calendar", "start", "end", "all_day", "location", "link"}


def test_only_the_tray_may_read_the_agenda():
    assert authorize(_W + "tray", "", "GET", "/v1/calendar/upcoming", ()) is None
    assert authorize(_W + "felt-animation", "", "GET", "/v1/calendar/upcoming", ()) is not None
    assert authorize(_W + "tray", "", "POST", "/v1/calendar/upcoming", ()) is not None
