"""llm_gate: one felt-llm call at a time, chat first, ambient gives way."""

import asyncio

import pytest

from felt_core import llm_gate


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch):
    monkeypatch.setattr(llm_gate, "_gate", llm_gate._Gate())


def _job(log: list, name: str, hold: asyncio.Event | None = None):
    async def call():
        log.append(f"start {name}")
        if hold:
            await hold.wait()
        log.append(f"end {name}")
        return name

    return call


async def _as(mark, call):
    with mark:
        return await llm_gate.run(call)


async def test_chat_then_timely_then_ambient_and_one_at_a_time():
    log, hold = [], asyncio.Event()
    first = asyncio.create_task(_as(llm_gate.maintenance("diary"), _job(log, "diary", hold)))
    await asyncio.sleep(0)
    ambient = asyncio.create_task(_as(llm_gate.ambient("song"), _job(log, "song")))
    timely = asyncio.create_task(_as(llm_gate.timely("alarm"), _job(log, "alarm")))
    await asyncio.sleep(0)
    assert log == ["start diary"]
    hold.set()
    assert await timely == "alarm"
    assert await ambient == "song"
    await first
    assert log == ["start diary", "end diary", "start alarm", "end alarm", "start song", "end song"]


async def test_chat_cancels_a_running_ambient_call_and_goes_next():
    log, hold = [], asyncio.Event()
    ambient = asyncio.create_task(_as(llm_gate.ambient("screen"), _job(log, "screen", hold)))
    while not log:
        await asyncio.sleep(0)
    assert await llm_gate.run(_job(log, "chat")) == "chat"
    with pytest.raises(llm_gate.Skipped, match="cancelled"):
        await ambient
    assert log == ["start screen", "start chat", "end chat"]


async def test_no_ambient_right_after_a_chat():
    await llm_gate.run(_job([], "chat"))
    with pytest.raises(llm_gate.Skipped, match="conversation"):
        await _as(llm_gate.ambient("song"), _job([], "song"))
    assert await _as(llm_gate.timely("alarm"), _job([], "alarm")) == "alarm"


async def test_ambient_budget(monkeypatch):
    monkeypatch.setattr(llm_gate, "AMBIENT_BUDGET_S", 0.0)
    with pytest.raises(llm_gate.Skipped, match="budget"):
        await _as(llm_gate.ambient("song"), _job([], "song"))


async def test_ambient_that_cannot_start_in_time_is_dropped():
    hold = asyncio.Event()
    busy = asyncio.create_task(_as(llm_gate.timely("alarm"), _job([], "alarm", hold)))
    await asyncio.sleep(0)
    with pytest.raises(llm_gate.Skipped, match="expired"):
        await _as(llm_gate.ambient("song", lifetime_s=0.05), _job([], "song"))
    hold.set()
    await busy


async def test_unmarked_calls_are_chat_and_errors_pass_through():
    async def boom():
        raise RuntimeError("felt-llm said no")

    with pytest.raises(RuntimeError):
        await llm_gate.run(boom)
    assert llm_gate._gate.chat_active == 0 and llm_gate._gate.running is None


def test_a_skipped_screen_reaction_is_no_reaction_not_an_outage(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from felt_core import local_llm
    from felt_core.api import app

    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))

    async def gave_way(*_a, **_k):
        raise local_llm.AmbientSkipped("ambient call skipped (conversation)")

    monkeypatch.setattr(local_llm, "chat", gave_way)
    with TestClient(app) as c:
        r = c.post(
            "/v1/react/screen",
            files={"image": ("s.jpg", b"jpeg", "image/jpeg")},
            data={"app": "firefox", "title": "wiki", "trigger": "periodic"},
        )
    assert r.status_code == 200 and r.content == b""
    assert r.headers["X-Reaction-Skipped"] == "llm-gate"
