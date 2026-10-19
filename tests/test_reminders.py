"""WP-46 reminders: events in the reminders calendar (through HA), which the
phone notifies for, spoken at the desk on time (calendar_headsup's REMINDER
stage)."""

import json
from datetime import UTC, datetime, timedelta

from conftest import load_plugin_module

from felt_core import calendar_headsup as ch
from felt_core import ha_calendar
from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext


def _config(tmp_path):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), ha_url="http://ha", ha_token="r")


async def _run(monkeypatch, tmp_path, params):
    skill = load_plugin_module("set_reminder/skill.py")
    created = []

    async def create(config, calendar_id, summary, start, end, description=None, **kw):
        created.append({"calendar": calendar_id, "summary": summary, "start": start, **kw})

    monkeypatch.setattr(skill, "create_event", create)
    ctx = ExecutionContext(config=_config(tmp_path), original_message="")
    result = await skill.handle(params, ctx)
    return created, result.text


async def test_a_one_off_reminder(monkeypatch, tmp_path):
    created, text = await _run(
        monkeypatch, tmp_path, {"what": "call the dentist", "when": "in 20 minutes"}
    )
    [event] = created
    assert event["summary"] == "call the dentist"
    assert event["calendar"] == "calendar.felt_reminders" and event["rrule"] is None
    assert text.startswith("I'll remind you") and "call the dentist" in text


async def test_a_weekly_reminder(monkeypatch, tmp_path):
    created, text = await _run(
        monkeypatch,
        tmp_path,
        {"what": "take the bins out", "when": "monday at 9am", "repeat": "weekly"},
    )
    assert created[0]["rrule"] == "FREQ=WEEKLY"
    assert "every Monday at 9:00 AM" in text


async def test_a_past_one_off_is_refused(monkeypatch, tmp_path):
    created, text = await _run(monkeypatch, tmp_path, {"what": "x", "when": "yesterday at 9am"})
    assert created == [] and "past" in text


async def test_an_unknown_repeat_is_once(monkeypatch, tmp_path):
    created, _ = await _run(
        monkeypatch, tmp_path, {"what": "x", "when": "in 1 hour", "repeat": "fortnightly"}
    )
    assert created[0]["rrule"] is None


async def test_create_event_message(monkeypatch, tmp_path):
    sent = []

    async def ws_call(config, message):
        sent.append(json.loads(json.dumps(message)))
        return {"success": True}

    monkeypatch.setattr(ha_calendar, "_ws_call", ws_call)
    start = datetime(2026, 10, 19, 9, 0, tzinfo=ha_calendar.TZ)
    await ha_calendar.create_event(
        _config(tmp_path),
        "calendar.felt_reminders",
        "bins",
        start,
        start + timedelta(minutes=15),
        rrule="FREQ=WEEKLY",
    )
    assert sent == [
        {
            "type": "calendar/event/create",
            "entity_id": "calendar.felt_reminders",
            "event": {
                "summary": "bins",
                "dtstart": "2026-10-19T09:00:00+01:00",
                "dtend": "2026-10-19T09:15:00+01:00",
                "rrule": "FREQ=WEEKLY",
            },
        }
    ]


# --- spoken at the desk, on time ---------------------------------------------


def _event(start, reminder=True):
    event = {"id": "e1", "summary": "call the dentist", "start": {"dateTime": start.isoformat()}}
    if reminder:
        event["_reminder"] = True
    return event


def test_a_reminder_gets_one_on_time_stage():
    now = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)
    start = now + timedelta(minutes=40)
    planned = ch.plan([("cal", _event(start))], now, 3600, lambda key: False)
    assert [(stage, round(delay)) for delay, _, _, stage in planned] == [(ch.REMINDER, 2400)]


def test_a_plain_event_keeps_its_heads_ups():
    now = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)
    planned = ch.plan(
        [("cal", _event(now + timedelta(minutes=40), reminder=False))], now, 3600, lambda k: False
    )
    assert [stage for _, _, _, stage in planned] == [ch.NORMAL, ch.URGENT]


def test_reminder_due_window():
    start = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
    assert ch.due_stage(start, start - timedelta(seconds=30), reminder=True) is ch.REMINDER
    assert ch.due_stage(start, start + timedelta(minutes=3), reminder=True) is ch.REMINDER
    assert ch.due_stage(start, start + timedelta(minutes=6), reminder=True) is None
    assert ch.due_stage(start, start - timedelta(minutes=5), reminder=True) is None


def test_reminder_prompt_and_fallback():
    assert "remind him now" in ch.build_prompt("call the dentist", 0, None, None, reminder=True)
    assert ch.fallback_line("call the dentist", 0, reminder=True) == "reminder: call the dentist."


async def test_a_day_without_a_time_is_nine_am(monkeypatch, tmp_path):
    # live 2026-10-18: "set an reminder for 21th to check on teh semantic router"
    created, _ = await _run(
        monkeypatch, tmp_path, {"what": "check on the semantic router", "when": "the 21st"}
    )
    start = created[0]["start"]
    assert (start.day, start.hour, start.minute) == (21, 9, 0)


async def test_a_given_time_is_kept(monkeypatch, tmp_path):
    created, _ = await _run(monkeypatch, tmp_path, {"what": "x", "when": "friday at 3pm"})
    assert created[0]["start"].hour == 15
