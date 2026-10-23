"""An appointment's place (2026-10-19), Mario's four cases:
1. appointment -> "where?" -> "none": added at once, no place
2. appointment + place: added at once, with it
3. appointment -> "where?" -> place: added once, with it
4. appointment -> "where?" -> nothing: added after 30 s, no place, a short line
plus a late answer (after the 30 s) changing the one already added."""

import asyncio
from datetime import datetime, timedelta

import pytest
from conftest import load_plugin_module

from felt_core import appointment_hold, ha_calendar, local_llm
from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext


def _ctx(tmp_path):
    config = FeltConfig(db_path=str(tmp_path / "felt.db"), ha_url="http://ha", ha_token="t")
    return ExecutionContext(config=config, original_message="")


class FakeCalendar:
    """HA's calendar as the skills see it: events in memory."""

    def __init__(self):
        self.events: list[dict] = []
        self.calls: list[str] = []
        self.spans: list[tuple] = []
        self.confirm = True
        self.down = False
        # her proactive lines
        self.lines: list[str] = []
        self._n = 0

    async def create(self, config, calendar_id, summary, start, end, description=None, **kw):
        if self.down:
            raise ha_calendar.CalendarUnavailable("down")
        self._n += 1
        self.calls.append(f"create {kw.get('location')}")
        self.spans.append((start, end, kw.get("all_day", False)))
        if self.confirm:
            self.events.append(
                {
                    "uid": f"u{self._n}",
                    "summary": summary,
                    "start": {"dateTime": start.isoformat()},
                    "location": kw.get("location"),
                }
            )

    async def delete(self, config, calendar_id, uid):
        self.calls.append(f"delete {uid}")
        self.events = [e for e in self.events if e["uid"] != uid]

    async def list_events(self, client, config, calendar_id, start, end):
        return list(self.events)

    async def refresh(self, client, config, calendar_id):
        pass

    async def calendars(self, client, config):
        return [{"id": "calendar.mario", "summary": "Mario", "primary": True}]

    async def writable(self, client, config, calendar_id):
        return True


@pytest.fixture
async def cal(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(ha_calendar, "create_event", fake.create)
    monkeypatch.setattr(appointment_hold, "enqueue_proactive", lambda c, k, t: fake.lines.append(t))

    async def llm_down(*a, **kw):  # the plain lines; test_her_words_for_the_no_place_add has hers
        raise local_llm.LocalLLMUnavailable("down")

    monkeypatch.setattr("felt_core.brain.replies.local_reply", llm_down)
    yield fake
    appointment_hold.claim()  # nothing left held between tests


def _skills(monkeypatch, fake):
    create = load_plugin_module("create_calendar_event/skill.py")
    place = load_plugin_module("set_event_location/skill.py")
    for name, fn in [
        ("create_event", fake.create),
        ("list_calendars", fake.calendars),
        ("writable", fake.writable),
    ]:
        monkeypatch.setattr(create, name, fn)
    # the place change (appointment_hold.change_place) talks to HA through ha_calendar
    for name, fn in [
        ("delete_event", fake.delete),
        ("list_events", fake.list_events),
        ("refresh", fake.refresh),
    ]:
        monkeypatch.setattr(ha_calendar, name, fn)
    return create, place


DENTIST = {"title": "dentist", "when": "tomorrow at 11am"}


async def test_a_day_without_a_time_is_all_day_and_not_asked_where(monkeypatch, tmp_path, cal):
    """cm-0vl: 'make an appointment so i know 4th november is WoW forever release'
    was an hour at midnight and a 'where is it?'."""
    create, _ = _skills(monkeypatch, cal)
    said = await create.handle(
        {"title": "WoW Forever release", "when": "4th november"}, _ctx(tmp_path)
    )
    assert cal.calls == ["create None"]
    start, end, all_day = cal.spans[0]
    assert all_day and (start.hour, start.minute) == (0, 0) and end - start == timedelta(days=1)
    assert "all day" in (said.text or "") and "where" not in (said.text or "").lower()


async def test_an_hour_mario_never_said_is_still_all_day(monkeypatch, tmp_path, cal):
    """Live 2026-10-23: 'put the 4th of November in my calendar as WoW Forever
    release' -> felt-llm sent when='4 November 15:00' -> Wednesday 3pm + 'where?'."""
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    ctx.original_message = "put the 4th of November in my calendar as WoW Forever release"
    said = await create.handle({"title": "WoW Forever release", "when": "4 November 15:00"}, ctx)
    start, end, all_day = cal.spans[0]
    assert all_day and (start.hour, start.minute) == (0, 0) and end - start == timedelta(days=1)
    assert "where" not in (said.text or "").lower()


async def test_1_none_adds_it_at_once_without_a_place(monkeypatch, tmp_path, cal):
    create, place = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    asked = await create.handle(DENTIST, ctx)
    assert "Ask Mario where it is" in (asked.text or "") and cal.calls == []

    answer = await place.handle({"place": "none"}, ctx)
    assert cal.calls == ["create None"]
    assert "no place" in (answer.text or "") and "Read it back" in (answer.text or "")
    await asyncio.sleep(0)
    assert cal.lines == []  # he answered: no extra line


async def test_1_none_through_create_again_is_the_answer(monkeypatch, tmp_path, cal):
    # e2e 2026-10-19: felt-llm answered "none" by calling create_calendar_event
    # again - that's his answer, not a second appointment at "none"
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    answer = await create.handle({**DENTIST, "place": "none"}, ctx)
    assert cal.calls == ["create None"] and "no place" in (answer.text or "")
    assert appointment_hold.claim() is None


async def test_1_create_again_without_a_place_is_the_answer(monkeypatch, tmp_path, cal):
    # e2e 2026-10-19: after "none" felt-llm called create again with no place at all
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    answer = await create.handle(DENTIST, ctx)
    assert cal.calls == ["create None"] and "no place" in (answer.text or "")
    assert appointment_hold.claim() is None


async def test_3_a_place_through_create_again_is_the_answer(monkeypatch, tmp_path, cal):
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    await create.handle({**DENTIST, "place": "Clínica Sorriso"}, ctx)
    assert cal.calls == ["create Clínica Sorriso"]


async def test_online_up_front_adds_it_without_asking(monkeypatch, tmp_path, cal):
    create, _ = _skills(monkeypatch, cal)
    result = await create.handle({**DENTIST, "place": "online"}, _ctx(tmp_path))
    assert cal.calls == ["create None"] and "no place" in (result.text or "")
    assert appointment_hold.claim() is None


async def test_2_a_place_up_front_is_added_at_once(monkeypatch, tmp_path, cal):
    create, _ = _skills(monkeypatch, cal)
    result = await create.handle({**DENTIST, "place": "Clínica Sorriso"}, _ctx(tmp_path))
    assert cal.calls == ["create Clínica Sorriso"]
    assert "at Clínica Sorriso" in (result.text or "") and "ask" not in (result.text or "")


async def test_3_the_answer_adds_it_once_with_the_place(monkeypatch, tmp_path, cal):
    create, place = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    answer = await place.handle({"place": "Clínica Sorriso, Gaia"}, ctx)
    assert cal.calls == ["create Clínica Sorriso, Gaia"]  # one add, no delete
    assert "at Clínica Sorriso, Gaia" in (answer.text or "")
    assert appointment_hold.claim() is None  # the timer is off


async def test_4_no_answer_adds_it_after_the_wait_and_says_so(monkeypatch, tmp_path, cal):
    monkeypatch.setattr(appointment_hold, "PLACE_WAIT_S", 0.01)
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    # cm-a4b: her reply (the question) can take a while - the wait hasn't started
    await asyncio.sleep(0.05)
    assert cal.calls == []
    appointment_hold.start_wait(ctx.config)  # chat(): her reply is out
    await asyncio.sleep(0.05)
    assert cal.calls == ["create None"]
    [line] = cal.lines
    assert line.startswith("Added dentist, ") and line.endswith(", 11:00, no place.")


async def test_a_late_answer_changes_the_one_already_added(monkeypatch, tmp_path, cal):
    create, place = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    await appointment_hold.add_unanswered(ctx.config)  # the 30 s ran out
    answer = await place.handle({"place": "Clínica Sorriso"}, ctx)
    # the new one first, the old one deleted only after
    assert cal.calls == ["create None", "create Clínica Sorriso", "delete u1"]
    [event] = cal.events
    assert event["location"] == "Clínica Sorriso" and "is now at" in (answer.text or "")


async def test_none_through_create_after_the_wait_adds_nothing_more(monkeypatch, tmp_path, cal):
    # cm-2hu (e2e 2026-10-23): it was already in when "none" came through
    # create_calendar_event again - it made a second one
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    await appointment_hold.add_unanswered(ctx.config)  # the 30 s ran out
    answer = await create.handle({**DENTIST, "place": "none"}, ctx)
    assert cal.calls == ["create None"] and "already in the calendar" in (answer.text or "")


async def test_none_through_create_after_a_made_up_place_adds_nothing_more(
    monkeypatch, tmp_path, cal
):
    # cm-2hu, the other way in: turn 1 came with a place, so it went in at once
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle({**DENTIST, "place": "dentist"}, ctx)
    await create.handle({**DENTIST, "place": "none"}, ctx)
    await create.handle(DENTIST, ctx)
    assert cal.calls == ["create dentist"]


async def test_a_late_place_through_create_changes_the_one_added(monkeypatch, tmp_path, cal):
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    await appointment_hold.add_unanswered(ctx.config)
    answer = await create.handle({**DENTIST, "place": "Clínica Sorriso"}, ctx)
    assert cal.calls == ["create None", "create Clínica Sorriso", "delete u1"]
    assert "is now at Clínica Sorriso" in (answer.text or "")


async def test_the_same_time_later_is_a_new_appointment(monkeypatch, tmp_path, cal):
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle({**DENTIST, "place": "Clínica Sorriso"}, ctx)
    monkeypatch.setattr(appointment_hold, "JUST_ADDED", timedelta(0))
    await create.handle({**DENTIST, "place": "Clínica Sorriso"}, ctx)
    assert cal.calls == ["create Clínica Sorriso", "create Clínica Sorriso"]


async def test_another_appointment_at_the_same_time_is_its_own(monkeypatch, tmp_path, cal):
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle({**DENTIST, "place": "Clínica Sorriso"}, ctx)
    await create.handle({"title": "call mum", "when": "tomorrow at 11am", "place": "none"}, ctx)
    assert cal.calls == ["create Clínica Sorriso", "create None"]


async def test_a_reworded_title_is_still_the_same_one(monkeypatch, tmp_path, cal):
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle({**DENTIST, "place": "Clínica Sorriso"}, ctx)
    await create.handle({**DENTIST, "title": "Dentist appointment", "place": "none"}, ctx)
    assert cal.calls == ["create Clínica Sorriso"]


async def test_a_late_none_changes_nothing(monkeypatch, tmp_path, cal):
    create, place = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    await appointment_hold.add_unanswered(ctx.config)
    answer = await place.handle({"place": "doesn't matter"}, ctx)
    assert cal.calls == ["create None"] and "stays without a place" in (answer.text or "")


async def test_a_second_appointment_adds_the_waiting_one_now(monkeypatch, tmp_path, cal):
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    await create.handle({"title": "haircut", "when": "tuesday at 3pm"}, ctx)
    await asyncio.sleep(0.01)
    assert cal.calls == ["create None"] and cal.lines[0].startswith("Added dentist")
    assert appointment_hold.claim() is not None  # the haircut waits for its place


async def test_unanswered_and_down_she_says_she_couldnt(monkeypatch, tmp_path, cal):
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    cal.down = True
    await appointment_hold.add_unanswered(ctx.config)
    [line] = cal.lines
    assert line.startswith("I couldn't add dentist")


async def test_answered_and_down_one_error_only(monkeypatch, tmp_path, cal):
    create, place = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    cal.down = True
    answer = await place.handle({"place": "Gaia"}, ctx)
    assert "couldn't reach" in (answer.text or "") and cal.lines == []


async def test_nothing_waiting_or_just_added(monkeypatch, tmp_path, cal):
    _, place = _skills(monkeypatch, cal)
    result = await place.handle({"place": "Gaia"}, _ctx(tmp_path))
    assert "say the whole thing again" in (result.text or "") and cal.calls == []


async def test_unconfirmed_change_keeps_the_old_one(monkeypatch, tmp_path, cal):
    create, place = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    await appointment_hold.add_unanswered(ctx.config)
    cal.confirm = False  # HA took the create but it doesn't show up
    result = await place.handle({"place": "Gaia"}, ctx)
    assert "couldn't confirm" in (result.text or "")
    assert [c for c in cal.calls if c.startswith("delete")] == [] and len(cal.events) == 1


def test_the_late_answer_window_is_two_hours(tmp_path):
    config = _ctx(tmp_path).config
    start = datetime.now(ha_calendar.TZ)
    ha_calendar.remember_created(config, "calendar.mario", "x", start, start)
    assert ha_calendar.last_created(config, start + timedelta(hours=1)) is not None
    assert ha_calendar.last_created(config, start + timedelta(hours=2, minutes=1)) is None


@pytest.mark.parametrize(
    ("answer", "none"),
    [
        ("none", True),
        ("No.", True),
        ("it's online", True),
        ("doesn't matter", True),
        ("no location", True),
        ("Clínica Sorriso", False),
        ("Nova Clínica", False),
        ("Rua Nova 5", False),
    ],
)
def test_no_place_answers(answer, none):
    assert appointment_hold.no_place(answer) is none


def test_read_back_format():
    start = datetime(2026, 10, 20, 11, 0, tzinfo=ha_calendar.TZ)
    assert appointment_hold.when(start) == "Tuesday 20 October, 11:00"


async def test_her_words_for_the_no_place_add(monkeypatch, tmp_path, cal):
    prompts: list[str] = []

    async def her(messages, config, *, include_facts):
        prompts.append(messages[-1]["content"])
        return "Dentist's in, no place though - you went quiet on me."

    monkeypatch.setattr("felt_core.brain.replies.local_reply", her)
    create, _ = _skills(monkeypatch, cal)
    ctx = _ctx(tmp_path)
    await create.handle(DENTIST, ctx)
    await appointment_hold.add_unanswered(ctx.config)
    assert cal.lines == ["Dentist's in, no place though - you went quiet on me."]
    assert '"dentist"' in prompts[0] and "without a place" in prompts[0]
