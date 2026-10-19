from datetime import datetime, timedelta

from felt_core.alarms import (
    _fire_at,
    alarm_facts,
    plain_alarm,
    reschedule_pending_alarms,
    schedule_alarm,
)
from felt_core.config import FeltConfig
from felt_core.db import get_pending_alarms, save_alarm


def _config(tmp_path) -> FeltConfig:
    # No ntfy_* vars set, so notify() is a real no-network no-op (same
    # pattern notify.py itself documents) — these tests exercise the
    # scheduling/persistence logic, not the actual push.
    return FeltConfig(db_path=str(tmp_path / "felt.db"))


async def test_schedule_alarm_persists_a_pending_row(tmp_path):
    # schedule_alarm calls asyncio.create_task, which needs a running
    # loop — hence an async test, even though we only check persistence.
    config = _config(tmp_path)
    fire_at = datetime.now() + timedelta(hours=8)
    schedule_alarm(config, fire_at, "wake up")
    pending = get_pending_alarms(config.db_path)
    assert len(pending) == 1
    assert pending[0]["label"] == "wake up"
    assert pending[0]["fire_at"] == fire_at.isoformat()


async def test_fire_at_marks_alarm_fired_once_due(tmp_path, fake_llm):
    config = _config(tmp_path)
    alarm_id = save_alarm(config.db_path, datetime.now().isoformat(), "test")
    # Already in the past, so _fire_at fires immediately without sleeping.
    await _fire_at(config, alarm_id, datetime.now() - timedelta(seconds=1), "test")
    assert get_pending_alarms(config.db_path) == []


async def test_reschedule_pending_alarms_schedules_one_task_per_pending_row(tmp_path, monkeypatch):
    config = _config(tmp_path)
    save_alarm(config.db_path, (datetime.now() + timedelta(hours=1)).isoformat(), "one")
    save_alarm(config.db_path, (datetime.now() + timedelta(hours=2)).isoformat(), "two")

    scheduled = []
    monkeypatch.setattr("felt_core.alarms.asyncio.create_task", lambda coro: scheduled.append(coro))

    reschedule_pending_alarms(config)

    assert len(scheduled) == 2
    for coro in scheduled:
        coro.close()  # avoid "coroutine was never awaited" warnings


def test_alarm_facts():
    at = datetime(2026, 10, 20, 7, 30)
    assert alarm_facts("stretch", at, at) == (
        "Mario's alarm is going off now, to remind him: \"stretch\" - say what it's for."
    )
    late = alarm_facts("", at, at + timedelta(hours=2))
    assert late.startswith("Mario's alarm is going off now.") and "07:30" in late and "late" in late
    assert plain_alarm("stretch") == "Alarm: stretch" and plain_alarm("") == "Alarm"


async def test_alarm_in_her_words(tmp_path, monkeypatch, fake_llm):
    config = _config(tmp_path)
    sent: list[str] = []

    async def fake_notify(config, text, title=None, priority=3):
        sent.append(text)
        return True

    monkeypatch.setattr("felt_core.alarms.notify", fake_notify)
    fake_llm.script = ['"Up you get - stretch time!"']
    alarm_id = save_alarm(config.db_path, datetime.now().isoformat(), "stretch")
    await _fire_at(config, alarm_id, datetime.now(), "stretch")
    assert sent == ["Up you get - stretch time!"]
    assert '"stretch"' in fake_llm.calls[0]["messages"][-1]["content"]


async def test_alarm_plain_when_felt_llm_is_down(tmp_path, monkeypatch, fake_llm):
    from felt_core import local_llm

    config = _config(tmp_path)
    sent: list[str] = []

    async def fake_notify(config, text, title=None, priority=3):
        sent.append(text)
        return True

    monkeypatch.setattr("felt_core.alarms.notify", fake_notify)
    fake_llm.script = [local_llm.LocalLLMUnavailable("down")]
    alarm_id = save_alarm(config.db_path, datetime.now().isoformat(), "stretch")
    await _fire_at(config, alarm_id, datetime.now(), "stretch")
    assert sent == ["Alarm: stretch"]
