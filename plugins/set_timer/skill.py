"""Timers — a real timer in the phone's Clock app (phone.py, via the Home
Assistant app, 2026-10-18), so it rings on the phone. If the phone can't
be reached, the old fallback: a push notification (notify.py) when it
elapses.

v1, deliberately simple: an in-process asyncio task, not persisted — a
felt-core restart loses pending timers. Good enough for "remind me in 10
minutes"; a real calendar/reminders system (assistant-skills.md) would
need SQLite-backed persistence to survive restarts, a bigger piece for
later.
"""

import asyncio

from felt_core.clients.notify import notify
from felt_core.clients.phone import PhoneUnavailableError, start_timer
from felt_core.core.params import get_single_param
from felt_core.models.plugin import SkillResult

name = "set_timer"
description = "Sets a countdown timer for N minutes on Mario's phone."
parameters_schema = {
    "type": "object",
    "properties": {
        "minutes": {"type": "number", "description": "Minutes from now"},
        "label": {"type": "string", "description": "What it's for"},
    },
    "required": ["minutes"],
}


# The event loop keeps only a weak reference to a task: a fallback timer nobody holds could be collected before it
# rings. Held here until it's done.
_pending: set[asyncio.Task] = set()


async def _fire_after(config, minutes: float, label: str) -> None:
    await asyncio.sleep(minutes * 60)
    text = f"Timer done: {label}" if label else "Timer done."
    await notify(config, text, title="Felt timer")


async def handle(params, ctx):
    minutes = get_single_param(params, "minutes", "duration", "duration_minutes", "time")
    if minutes is None:
        return SkillResult(text="I need a number of minutes to set a timer.")
    label = params.get("label", "")

    minutes = float(minutes)
    try:
        await start_timer(ctx.config, max(1, round(minutes * 60)), label)
        return SkillResult(text=f"Timer set on your phone for {minutes:g} minutes.")
    except PhoneUnavailableError:
        pass  # phone offline: a push when it's done instead
    task = asyncio.create_task(_fire_after(ctx.config, minutes, label))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return SkillResult(text=f"Timer set for {minutes:g} minutes (phone unreachable - I'll ping you).")
