"""Mario 10-03: she lives on the server, so any sign of him (a message from
any device, an app window opening) can start her day - not only the desktop
character's day_start."""

import asyncio
from datetime import UTC, datetime

import pytest

from felt_core import diary


class Config:
    def __init__(self, db_path):
        self.db_path = db_path


@pytest.fixture(autouse=True)
def fresh_state():
    diary._tried.clear()
    yield
    diary._tried.clear()


def _calls(monkeypatch):
    calls = []

    async def fake_on_event(config, kind, at):
        calls.append((kind, at))

    monkeypatch.setattr(diary, "on_event", fake_on_event)
    return calls


def _run(config, now):
    async def go():
        diary.he_is_here(config, now)
        await asyncio.sleep(0)

    asyncio.run(go())


def test_a_morning_sign_writes_yesterday_once(tmp_path, monkeypatch):
    calls = _calls(monkeypatch)
    config = Config(str(tmp_path / "f.db"))
    morning = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
    _run(config, morning)
    _run(config, morning.replace(minute=5))  # within RETRY_S: not again
    assert calls == [("day_start", "2026-10-03 08:00:00")]
    _run(config, morning.replace(minute=20))  # nothing written yet: retried
    assert len(calls) == 2


def test_nothing_before_five_or_once_the_entry_exists(tmp_path, monkeypatch):
    calls = _calls(monkeypatch)
    config = Config(str(tmp_path / "f.db"))
    _run(config, datetime(2026, 10, 3, 3, 0, tzinfo=UTC))  # 04:00 in Lisbon
    from felt_core.db import insert_diary

    insert_diary(config.db_path, "2026-10-02", "A day.", "[]", "calm")
    _run(config, datetime(2026, 10, 3, 9, 0, tzinfo=UTC))
    assert calls == []
