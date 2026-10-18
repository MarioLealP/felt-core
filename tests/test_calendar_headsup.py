"""WP-31: calendar heads-up - stage timing, planning, dedupe, the
re-check before delivery, and the enqueued proactive item."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from felt_core import calendar_headsup as ch
from felt_core import db, local_llm
from felt_core.config import FeltConfig

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _config(tmp_path):
    return FeltConfig(db_path=str(tmp_path / "felt.db"), google_calendar_refresh_token="x")


def _event(minutes_out: float, event_id="ev1", **extra):
    start = NOW + timedelta(minutes=minutes_out)
    return {"id": event_id, "summary": "Dentist", "start": {"dateTime": start.isoformat()}, **extra}


@pytest.fixture(autouse=True)
def _clear_pending():
    ch._pending.clear()
    yield
    ch._pending.clear()


# -- pure ------------------------------------------------------------------


def test_due_stage_boundaries():
    assert ch.due_stage(NOW + timedelta(minutes=45), NOW) is None
    assert ch.due_stage(NOW + timedelta(minutes=30), NOW) is ch.NORMAL
    assert ch.due_stage(NOW + timedelta(minutes=12), NOW) is ch.NORMAL
    assert ch.due_stage(NOW + timedelta(minutes=10), NOW) is ch.URGENT
    assert ch.due_stage(NOW + timedelta(minutes=1), NOW) is ch.URGENT
    assert ch.due_stage(NOW, NOW) is None


def test_all_day_cancelled_and_declined_events_are_skipped():
    assert not ch.is_relevant({"id": "a", "start": {"date": "2026-10-01"}})
    assert not ch.is_relevant(_event(20, status="cancelled"))
    assert not ch.is_relevant(_event(20, attendees=[{"self": True, "responseStatus": "declined"}]))
    assert ch.is_relevant(_event(20, attendees=[{"self": True, "responseStatus": "accepted"}]))


def test_plan_schedules_stages_firing_before_the_next_poll():
    # 12 min out: the 30-min stage's fire time has passed but it's still in
    # its window (delay 0, sent now); the 10-min stage fires in 2 min.
    planned = ch.plan([("cal", _event(12))], NOW, 330, lambda key: False)
    by_stage = {stage.name: delay for delay, _, _, stage in planned}
    assert by_stage == {"30": 0.0, "10": 120.0}


def test_plan_ignores_far_events_and_sent_or_pending_keys():
    assert ch.plan([("cal", _event(90))], NOW, 330, lambda key: False) == []
    event = _event(12)
    ch._pending.add(ch.dedupe_key(event, ch.URGENT))
    planned = ch.plan(
        [("cal", event)], NOW, 330, lambda key: key == ch.dedupe_key(event, ch.NORMAL)
    )
    assert planned == []


def test_moved_event_gets_a_new_dedupe_key():
    assert ch.dedupe_key(_event(30), ch.URGENT) != ch.dedupe_key(_event(45), ch.URGENT)


def test_current_game_is_the_unstopped_game_start(tmp_path):
    config = _config(tmp_path)
    at = (NOW - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    db.save_event(config.db_path, "game_start", json.dumps({"name": "PathOfExile.exe"}), at)
    assert ch.current_game(config, NOW) == "PathOfExile"
    later = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    db.save_event(config.db_path, "game_stop", "{}", later)
    assert ch.current_game(config, NOW) is None


# -- deliver ---------------------------------------------------------------


def _patch(monkeypatch, fresh, line="dentist in ten, go"):
    async def fake_get_event(client, config, calendar_id, event_id):
        return fresh

    prompts = []

    async def fake_local_reply(messages, config, *, include_facts, tone=None):
        prompts.append(messages[-1]["content"])
        if line is None:
            raise local_llm.LocalLLMUnavailable("down")
        return line

    monkeypatch.setattr(ch.google_calendar, "get_event", fake_get_event)
    monkeypatch.setattr("felt_core.brain.replies.local_reply", fake_local_reply)
    return prompts


def _queued(config):
    return db.claim_next_proactive(config.db_path)


async def test_urgent_heads_up_is_enqueued_once(tmp_path, monkeypatch):
    config = _config(tmp_path)
    event = _event(10)
    prompts = _patch(monkeypatch, event)

    assert await ch.deliver(config, "cal", event, ch.URGENT, now=NOW)
    item = _queued(config)
    assert item is not None
    assert item["kind"] == "urgent"
    assert item["text"] == "dentist in ten, go"
    assert "in 10 minutes" in prompts[0]
    # Same event, same stage: never twice, even after the queue drained.
    assert not await ch.deliver(config, "cal", event, ch.URGENT, now=NOW)
    assert _queued(config) is None


async def test_deleted_event_is_dropped(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _patch(monkeypatch, None)
    assert not await ch.deliver(config, "cal", _event(10), ch.URGENT, now=NOW)
    assert _queued(config) is None


async def test_moved_event_is_dropped_for_the_next_poll(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _patch(monkeypatch, _event(40))
    assert not await ch.deliver(config, "cal", _event(10), ch.URGENT, now=NOW)
    assert _queued(config) is None


async def test_stale_normal_stage_is_not_sent_late(tmp_path, monkeypatch):
    config = _config(tmp_path)
    event = _event(8)
    _patch(monkeypatch, event)
    assert not await ch.deliver(config, "cal", event, ch.NORMAL, now=NOW)


async def test_game_context_reaches_the_prompt(tmp_path, monkeypatch):
    config = _config(tmp_path)
    at = (NOW - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    db.save_event(config.db_path, "game_start", json.dumps({"name": "PathOfExile.exe"}), at)
    event = _event(25)
    prompts = _patch(monkeypatch, event)
    assert await ch.deliver(config, "cal", event, ch.NORMAL, now=NOW)
    assert "PathOfExile" in prompts[0]
    item = _queued(config)
    assert item is not None and item["kind"] == "calendar"


async def test_plain_line_when_her_model_is_down(tmp_path, monkeypatch):
    config = _config(tmp_path)
    event = _event(10)
    _patch(monkeypatch, event, line=None)
    assert await ch.deliver(config, "cal", event, ch.URGENT, now=NOW)
    item = _queued(config)
    assert item is not None
    assert item["text"] == "heads up, Dentist in 10 minutes."


async def test_loop_is_off_without_calendar(tmp_path):
    config = FeltConfig(db_path=str(tmp_path / "felt.db"), google_calendar_refresh_token=None)
    await ch.calendar_headsup_loop(config)  # returns instead of looping


async def test_poll_schedules_and_marks_pending(tmp_path, monkeypatch):
    config = _config(tmp_path)

    async def fake_fetch(config, now):
        return [("cal", _event(12)), ("cal", _event(90, event_id="later"))]

    started = []

    async def fake_deliver_later(config, delay, calendar_id, event, stage):
        started.append((delay, stage.name))

    monkeypatch.setattr(ch, "fetch_upcoming", fake_fetch)
    monkeypatch.setattr(ch, "_deliver_later", fake_deliver_later)

    assert await ch.poll_once(config, now=NOW) == 2
    for task in list(ch._tasks):
        await task
    assert sorted(started) == [(0.0, "30"), (120.0, "10")]
    # A second poll before delivery doesn't schedule them again.
    assert await ch.poll_once(config, now=NOW) == 0


# -- leave-by (2026-10-18) ---------------------------------------------------------

ADDRESS = "Hospital da Luz Arrábida, Praceta Henrique Moreira, 150, 4400-346 Vila Nova de Gaia"


def _trip(minutes_out: float, event_id="trip1", location=ADDRESS):
    return _event(minutes_out, event_id, summary="TAC", location=location)


def _patch_trip(monkeypatch, fresh, drive=30.0, line="time to head out"):
    prompts = _patch(monkeypatch, fresh, line)
    notified = []

    async def fake_drive(config, destination, now=None):
        return drive

    async def fake_notify(config, title, message, tag):
        notified.append((title, message, tag))

    monkeypatch.setattr(ch.leave_by, "drive_minutes", fake_drive)
    monkeypatch.setattr(ch.phone, "notify", fake_notify)
    return prompts, notified


@pytest.mark.parametrize(
    ("location", "address"),
    [
        (ADDRESS, True),
        ("Rua de Santa Catarina 112, Porto", True),
        ("Mom's place", False),
        ("Google Meet", False),
        ("", False),
        (None, False),
    ],
)
def test_has_address(location, address):
    assert ch.leave_by.has_address({"location": location}) is address


def test_leave_time_keeps_marios_buffer():
    # 30 min drive + 20 min buffer (Mario: "15-20min ... I prefer get there early")
    assert ch.leave_by.leave_time(NOW, 30) == NOW - timedelta(minutes=50)
    assert ch.leave_by.leave_time(NOW, 29.2) == NOW - timedelta(minutes=50)  # rounded up


def test_plan_gives_a_routable_event_leave_nudges_not_heads_ups():
    trip = _trip(80)  # leave at 80-30-20 = 30 out; "soon" at 15 out
    planned = ch.plan([("cal", trip)], NOW, 3600, lambda k: False, {"trip1": 30.0})
    assert [(st, round(d / 60)) for d, _, _, st in planned] == [
        (ch.LEAVE_SOON, 15),
        (ch.LEAVE_NOW, 30),
    ]


def test_an_unroutable_event_keeps_the_heads_ups():
    planned = ch.plan([("cal", _trip(25))], NOW, 3600, lambda k: False, {})
    assert {st for _, _, _, st in planned} == {ch.NORMAL, ch.URGENT}
    planned = ch.plan([("cal", _event(25))], NOW, 3600, lambda k: False, {"ev1": 10.0})
    assert {st for _, _, _, st in planned} == {ch.NORMAL, ch.URGENT}  # no address


def test_seen_late_only_time_to_go():
    trip = _trip(40)  # leave was 10 min ago (40-30-20)
    planned = ch.plan([("cal", trip)], NOW, 3600, lambda k: False, {"trip1": 30.0})
    assert [(st, d) for d, _, _, st in planned] == [(ch.LEAVE_NOW, 0.0)]


async def test_leave_soon_goes_to_desk_and_phone(tmp_path, monkeypatch):
    config = _config(tmp_path)
    trip = _trip(65)  # leave at 65-50 = 15 out
    prompts, notified = _patch_trip(monkeypatch, trip, line="leave in 15 for the TAC")
    assert await ch.deliver_leave(config, "cal", trip, ch.LEAVE_SOON, now=NOW)
    item = _queued(config)
    assert item["kind"] == "urgent" and item["text"] == "leave in 15 for the TAC"
    assert "leave in 15 minutes" in prompts[0] and "30 minutes" in prompts[0]
    assert notified == [("TAC", "leave in 15 for the TAC", "felt-leave-trip1")]
    assert not await ch.deliver_leave(config, "cal", trip, ch.LEAVE_SOON, now=NOW)  # once


async def test_traffic_eased_waits_for_the_new_time(tmp_path, monkeypatch):
    config = _config(tmp_path)
    trip = _trip(65)
    _patch_trip(monkeypatch, trip, drive=20.0)  # leave now 10 min later
    assert not await ch.deliver_leave(config, "cal", trip, ch.LEAVE_SOON, now=NOW)
    assert _queued(config) is None


async def test_traffic_worse_goes_out_now(tmp_path, monkeypatch):
    config = _config(tmp_path)
    trip = _trip(80)  # planned for 15 out at 30 min drive
    prompts, _ = _patch_trip(monkeypatch, trip, drive=45.0)  # leave now in 15
    assert await ch.deliver_leave(config, "cal", trip, ch.LEAVE_SOON, now=NOW)
    assert "45 minutes" in prompts[0]


async def test_time_to_go_plain_line_when_her_model_is_down(tmp_path, monkeypatch):
    config = _config(tmp_path)
    trip = _trip(50)
    _patch_trip(monkeypatch, trip, line=None)
    assert await ch.deliver_leave(config, "cal", trip, ch.LEAVE_NOW, now=NOW)
    assert _queued(config)["text"].startswith("time to go: TAC at ")


async def test_poll_asks_drive_times_only_for_addresses(tmp_path, monkeypatch):
    config = _config(tmp_path)
    asked = []

    async def fake_drive(config, destination, now=None):
        asked.append(destination)
        return 30.0

    monkeypatch.setattr(ch.leave_by, "drive_minutes", fake_drive)
    drive = await ch._drive_times(config, [("cal", _trip(80)), ("cal", _event(80, "plain"))], NOW)
    assert drive == {"trip1": 30.0} and asked == [ADDRESS]


async def test_an_appointment_entered_twice_is_one(tmp_path, monkeypatch):
    config = _config(tmp_path)
    twin_a, twin_b = _trip(80, "a"), _trip(80, "b")

    async def cals(client, config):
        return [{"id": "c1"}, {"id": "c2"}]

    async def events(client, config, cid, start, end):
        return [twin_a, _event(90, "other")] if cid == "c1" else [twin_b]

    monkeypatch.setattr(ch.google_calendar, "list_calendars", cals)
    monkeypatch.setattr(ch.google_calendar, "list_events", events)
    got = await ch.fetch_upcoming(config, NOW)
    assert [e["id"] for _, e in got] == ["a", "other"]
