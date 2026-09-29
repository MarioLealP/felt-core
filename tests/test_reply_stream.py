import asyncio

import pytest

from felt_core import reply_stream


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    reply_stream.clear()
    now = [1000.0]
    monkeypatch.setattr(reply_stream, "_clock", lambda: now[0])
    yield now
    reply_stream.clear()


async def test_chunk_zero_is_ready_immediately():
    s = reply_stream.create(["a", "b"], b"WAV0")
    assert await s.wait_chunk(0, 1) == b"WAV0"
    assert reply_stream.get(s.id) is s


async def test_waiter_wakes_when_the_chunk_lands():
    s = reply_stream.create(["a", "b"], b"WAV0")
    waiter = asyncio.create_task(s.wait_chunk(1, 30))
    await asyncio.sleep(0)
    assert not waiter.done()
    s.set_chunk(1, b"WAV1")
    assert await waiter == b"WAV1"


async def test_wait_times_out_with_none():
    s = reply_stream.create(["a", "b"], b"WAV0")
    assert await s.wait_chunk(1, 0.05) is None


async def test_run_fills_chunks_in_order():
    s = reply_stream.create(["a", "b", "c"], b"WAV0")
    seen = []

    async def synth(n, text):
        seen.append((n, text))
        return f"WAV{n}".encode()

    await reply_stream.run(s, synth)
    assert seen == [(1, "b"), (2, "c")]
    assert await s.wait_chunk(2, 1) == b"WAV2"


async def test_failure_fails_that_chunk_and_every_later_one():
    s = reply_stream.create(["a", "b", "c", "d"], b"WAV0")

    async def synth(n, text):
        if n == 2:
            raise RuntimeError("felt-voice down")
        return b"ok"

    waiter = asyncio.create_task(s.wait_chunk(3, 30))
    await reply_stream.run(s, synth)
    assert await s.wait_chunk(1, 1) == b"ok"
    with pytest.raises(reply_stream.ChunkFailed):
        await s.wait_chunk(2, 1)
    with pytest.raises(reply_stream.ChunkFailed):
        await waiter


async def test_expires_120s_after_creation(_fresh_store):
    s = reply_stream.create(["a", "b"], b"WAV0")
    _fresh_store[0] += 119
    assert reply_stream.get(s.id) is s
    _fresh_store[0] += 2
    assert reply_stream.get(s.id) is None


async def test_expiry_restarts_when_the_last_chunk_is_done(_fresh_store):
    s = reply_stream.create(["a", "b"], b"WAV0")
    _fresh_store[0] += 100
    s.set_chunk(1, b"WAV1")
    _fresh_store[0] += 100  # 200 s after creation, 100 s after completion
    assert reply_stream.get(s.id) is s
    _fresh_store[0] += 21
    assert reply_stream.get(s.id) is None


async def test_at_most_eight_streams_oldest_dropped():
    streams = [reply_stream.create(["a", "b"], b"W") for _ in range(9)]
    assert reply_stream.get(streams[0].id) is None
    assert all(reply_stream.get(s.id) is s for s in streams[1:])


async def test_dropping_a_stream_cancels_its_task():
    first = reply_stream.create(["a", "b"], b"W")
    first.task = asyncio.create_task(asyncio.sleep(60))
    for _ in range(8):
        reply_stream.create(["a", "b"], b"W")
    await asyncio.sleep(0)
    assert first.task.cancelled() or first.task.cancelling()
