"""Timers — the simplest possible proof of Felt's new push channel
(notify.py). Fires a real ntfy notification when the timer elapses.

v1, deliberately simple: an in-process asyncio task, not persisted — a
felt-core restart loses pending timers. Good enough for "remind me in 10
minutes"; a real calendar/reminders system (assistant-skills.md) would
need SQLite-backed persistence to survive restarts, a bigger piece for
later.
"""

import asyncio

from felt_core.notify import notify
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "set_timer"
description = "Sets a timer for N minutes from now; sends a push notification when it elapses."
parameters_schema = {
    "type": "object",
    "properties": {
        "minutes": {"type": "number", "description": "How many minutes from now"},
        "label": {"type": "string", "description": "What the timer is for"},
    },
    "required": ["minutes"],
}


async def _fire_after(config, minutes: float, label: str) -> None:
    await asyncio.sleep(minutes * 60)
    text = f"Timer done: {label}" if label else "Timer done."
    await notify(config, text, title="Felt timer")


async def handle(params, ctx):
    minutes = get_single_param(params, "minutes", "duration", "duration_minutes", "time")
    if minutes is None:
        return SkillResult(text="I need a number of minutes to set a timer.")
    label = params.get("label", "")

    asyncio.create_task(_fire_after(ctx.config, float(minutes), label))
    return SkillResult(text=f"Timer set for {minutes} minutes.")
