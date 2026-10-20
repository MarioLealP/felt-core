"""In-memory store for a streamed chat reply's later sentence chunks (WP-54).

/v1/voice-chat and /v1/text-chat answer with chunk 0's audio and register
the reply here; a background task fills chunks 1..N-1 in order while
felt-tray long-polls GET /v1/reply/{id}/chunk/{n}. Memory only, on purpose:
a stream is worth nothing once the reply has been spoken (or the process
restarted), so entries expire 120 s after the last chunk finished (or after
creation, if it never does) and at most 8 are kept, oldest dropped.

WP-65 (protocol v2): an *open* stream starts with only chunk 0 - the reply
is still being written - and gets each later chunk `append`ed as it comes,
then `end`ed with the finished reply's result (text, ui_action, tag), which
GET /v1/reply/{id} serves.
"""

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

TTL_S = 120.0
MAX_STREAMS = 8

# Injectable so the expiry tests don't need to sleep (the Windows tick is
# ~15.6 ms, and real sleeps of that size are flaky anyway).
_clock: Callable[[], float] = time.monotonic


class ChunkFailed(Exception):
    """The chunk's TTS failed (or an earlier chunk's did - a stream stops at
    the first failure, the tray can't play past a gap)."""


class ReplyStream:
    def __init__(self, stream_id: str, texts: list[str], *, open_: bool = False) -> None:
        self.id = stream_id
        self.texts = texts
        self._audio: list[bytes | None] = [None] * len(texts)
        self._failed: list[bool] = [False] * len(texts)
        self._events = [asyncio.Event() for _ in texts]
        self.expires_at = _clock() + TTL_S
        self.task: asyncio.Task[None] | None = None
        # WP-65: an open stream isn't done until `end`; a closed one (WP-54)
        # knows all its chunks from the start
        self.done = not open_
        self.result: dict | None = None
        self.failed_from: int | None = None
        self._grew = asyncio.Event()

    def __len__(self) -> int:
        return len(self.texts)

    def set_chunk(self, n: int, wav: bytes) -> None:
        self._audio[n] = wav
        self._events[n].set()
        if self.done and all(a is not None for a in self._audio):
            self.expires_at = _clock() + TTL_S

    def fail_from(self, n: int) -> None:
        """Marks chunk n and everything after it failed, waking waiters."""
        self.failed_from = n if self.failed_from is None else min(n, self.failed_from)
        for i in range(n, len(self)):
            self._failed[i] = True
            self._events[i].set()
        self.expires_at = _clock() + TTL_S

    def append(self, text: str) -> int:
        """An open stream's next chunk (its audio still to come); its index.
        Past a failure it's failed straight away."""
        n = len(self.texts)
        self.texts.append(text)
        self._audio.append(None)
        self._failed.append(False)
        self._events.append(asyncio.Event())
        if self.failed_from is not None:
            self.fail_from(self.failed_from)
        self.expires_at = _clock() + TTL_S
        self._wake()
        return n

    def end(self, result: dict) -> None:
        """No more chunks; `result` is what GET /v1/reply/{id} answers."""
        self.result = result
        self.done = True
        self.expires_at = _clock() + TTL_S
        self._wake()

    def _wake(self) -> None:
        self._grew.set()
        self._grew = asyncio.Event()

    async def wait_for_chunk_slot(self, n: int, timeout: float) -> bool:
        """Waits up to `timeout` s for chunk n to exist (an open stream
        grows); False if it doesn't by then or the stream ended before it."""
        try:
            async with asyncio.timeout(timeout):
                while n >= len(self) and not self.done:
                    await self._grew.wait()
        except TimeoutError:
            return False
        return n < len(self)

    async def wait_chunk(self, n: int, timeout: float) -> bytes | None:
        """The chunk's WAV, waiting up to `timeout` s for it; None if it's
        still not ready by then. Raises ChunkFailed. `n` must be < len."""
        if not self._events[n].is_set():
            try:
                await asyncio.wait_for(self._events[n].wait(), timeout)
            except TimeoutError:
                return None
        if self._failed[n]:
            raise ChunkFailed(n)
        return self._audio[n]


_streams: "OrderedDict[str, ReplyStream]" = OrderedDict()


def _purge() -> None:
    now = _clock()
    for sid in [sid for sid, s in _streams.items() if s.expires_at <= now]:
        _drop(sid)


def _drop(stream_id: str) -> None:
    stream = _streams.pop(stream_id, None)
    if stream is not None and stream.task is not None and not stream.task.done():
        stream.task.cancel()


def create(texts: list[str], first_wav: bytes) -> ReplyStream:
    """Registers a new stream whose chunk 0 (`texts[0]`) is already
    synthesised. The caller starts `run` on it for the rest."""
    _purge()
    stream = ReplyStream(uuid.uuid4().hex, texts)
    stream.set_chunk(0, first_wav)
    _register(stream)
    return stream


def open_stream(first_text: str, first_wav: bytes) -> ReplyStream:
    """WP-65: a stream that starts with chunk 0 only; see ReplyStream.append/end."""
    _purge()
    stream = ReplyStream(uuid.uuid4().hex, [first_text], open_=True)
    stream.set_chunk(0, first_wav)
    _register(stream)
    return stream


def _register(stream: ReplyStream) -> None:
    _streams[stream.id] = stream
    while len(_streams) > MAX_STREAMS:
        _drop(next(iter(_streams)))


def get(stream_id: str) -> ReplyStream | None:
    _purge()
    return _streams.get(stream_id)


def clear() -> None:
    for sid in list(_streams):
        _drop(sid)


async def run(stream: ReplyStream, synthesize: Callable[[int, str], Awaitable[bytes]]) -> None:
    """Fills chunks 1..N-1 in order. The first failure fails the rest of
    the stream (see ChunkFailed)."""
    for n in range(1, len(stream)):
        try:
            stream.set_chunk(n, await synthesize(n, stream.texts[n]))
        except Exception:
            logger.exception("reply stream %s: TTS failed for chunk %d", stream.id, n)
            stream.fail_from(n)
            return
