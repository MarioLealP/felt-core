"""One way onto felt-llm (Mario 2026-10-21: "go for it" on GPT's broker
review). felt-llm runs one request at a time; a cache probe that day had
unprompted background lines holding it ~80 % of the time, so a chat reply
waited 20-40 s behind a song remark. Every local_llm call now goes through
here, one at a time, by class:

- CHAT (default - anything not marked): Mario talking, the skills in his turn.
- TIMELY: alarms, calendar heads-ups, alert lines, print done - wait behind chat.
- AMBIENT: lines nobody asked for (screen, songs, desktop events, DJ, PoE).
- MAINTENANCE: the nightly diary, the warm-up.

Ambient is the only class that gives way:
- not started during a chat or for QUIET_AFTER_CHAT_S after his last turn
  (he's likely to say something next),
- not started past AMBIENT_BUDGET_S of model time in the last minute,
- dropped if it can't start within its lifetime (the song has changed),
- cancelled when a chat arrives while it runs. That frees the slot at once
  while felt-llm is writing; while it is still reading the prompt, felt-llm
  finishes the read first (measured 2026-10-21: 53 s on a 2.5K prompt), which
  is why the quiet window matters more than the cancel.

A skipped or cancelled ambient call raises Skipped here; local_llm turns it
into its AmbientSkipped, a LocalLLMUnavailable - every ambient feature already
treats that as "no line".
Each run is logged with its class, label, wait and duration.
"""

import asyncio
import contextvars
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

CHAT, TIMELY, AMBIENT, MAINTENANCE = 0, 1, 2, 3
_NAMES = {CHAT: "chat", TIMELY: "timely", AMBIENT: "ambient", MAINTENANCE: "maintenance"}

QUIET_AFTER_CHAT_S = 120.0
AMBIENT_BUDGET_S = 10.0
AMBIENT_WINDOW_S = 60.0
AMBIENT_LIFETIME_S = 60.0


class Skipped(Exception):
    """An ambient call that gave way to Mario (or ran out of budget/time)."""


_class: contextvars.ContextVar[tuple[int, str, float]] = contextvars.ContextVar(
    "llm_class", default=(CHAT, "chat", 0.0)
)


@contextmanager
def _marked(cls: int, label: str, lifetime_s: float = 0.0) -> Generator[None]:
    token = _class.set((cls, label, lifetime_s))
    try:
        yield
    finally:
        _class.reset(token)


def ambient(label: str, lifetime_s: float = AMBIENT_LIFETIME_S):
    return _marked(AMBIENT, label, lifetime_s)


def timely(label: str):
    return _marked(TIMELY, label)


def maintenance(label: str):
    return _marked(MAINTENANCE, label)


class _Gate:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.cond = asyncio.Condition()
        self.running: tuple[int, asyncio.Task] | None = None
        self.waiting: list[int] = []
        self.chat_active = 0
        self.last_chat = float("-inf")
        self.ambient_runs: deque[tuple[float, float]] = deque()  # (ended, seconds)

    def ambient_used(self, now: float) -> float:
        while self.ambient_runs and self.ambient_runs[0][0] < now - AMBIENT_WINDOW_S:
            self.ambient_runs.popleft()
        return sum(s for _, s in self.ambient_runs)

    def why_not_ambient(self, now: float) -> str | None:
        if self.chat_active or now - self.last_chat < QUIET_AFTER_CHAT_S:
            return "conversation"
        if self.ambient_used(now) >= AMBIENT_BUDGET_S:
            return "budget"
        return None

    def my_turn(self, cls: int) -> bool:
        return self.running is None and not any(w < cls for w in self.waiting)


_gate = _Gate()


def _current() -> _Gate:
    """The gate for this event loop (tests run several loops in one process)."""
    global _gate
    loop = asyncio.get_running_loop()
    if _gate.loop is not loop:
        _gate = _Gate()
        _gate.loop = loop
    return _gate


async def run[T](call: Callable[[], Awaitable[T]]) -> T:
    """Run one felt-llm call when it's this caller's turn (see the module doc)."""
    cls, label, lifetime_s = _class.get()
    gate = _current()
    name = f"{_NAMES[cls]} {label}" if label != "chat" else "chat"
    if cls == AMBIENT and (why := gate.why_not_ambient(time.monotonic())):
        logger.info("llm gate: %s skipped (%s)", name, why)
        raise Skipped(f"ambient call skipped ({why})")
    queued = time.monotonic()
    async with gate.cond:
        if cls == CHAT:
            gate.chat_active += 1
            if gate.running and gate.running[0] == AMBIENT:
                gate.running[1].cancel()
        gate.waiting.append(cls)
        try:
            timeout = lifetime_s if cls == AMBIENT else None
            await asyncio.wait_for(gate.cond.wait_for(lambda: gate.my_turn(cls)), timeout)
        except TimeoutError:
            logger.info("llm gate: %s dropped (no turn within %.0f s)", name, lifetime_s)
            raise Skipped("ambient call expired before its turn") from None
        except BaseException:
            if cls == CHAT:
                gate.chat_active -= 1
            raise
        finally:
            gate.waiting.remove(cls)
        if cls == AMBIENT and (why := gate.why_not_ambient(time.monotonic())):
            gate.cond.notify_all()
            logger.info("llm gate: %s skipped (%s)", name, why)
            raise Skipped(f"ambient call skipped ({why})")
        task = asyncio.ensure_future(call())
        gate.running = (cls, task)
    started = time.monotonic()
    try:
        return await task
    except asyncio.CancelledError:
        if task.cancelled() and cls == AMBIENT and not _cancelling():
            logger.info("llm gate: %s cancelled for a chat", name)
            raise Skipped("ambient call cancelled for a chat") from None
        raise
    finally:
        ended = time.monotonic()
        async with gate.cond:
            gate.running = None
            if cls == CHAT:
                gate.chat_active -= 1
                gate.last_chat = ended
            elif cls == AMBIENT:
                gate.ambient_runs.append((ended, ended - started))
            gate.cond.notify_all()
        logger.info(
            "llm gate: %s ran %.1f s (waited %.1f s)", name, ended - started, started - queued
        )


def _cancelling() -> bool:
    """Whether the caller itself is being cancelled (not just its felt-llm call)."""
    me = asyncio.current_task()
    return bool(me and me.cancelling())
