"""Time-of-day alarms — distinct from set_timer's duration-based
countdown (Mario, 2026-09-12: "another one skill i remembered was set an
alarm / timer", naming it as a separate, missing capability). Persisted
in SQLite, unlike set_timer's fire-and-forget in-process asyncio task:
an alarm set the night before has to survive a felt-core restart between
then and when it actually fires, so a pending row has to be reloaded and
rescheduled on startup, not just held in memory.
"""

import asyncio
import logging
from datetime import datetime

from felt_core import llm_gate
from felt_core.config import FeltConfig
from felt_core.db import (
    ALARM,
    APP_SESSION,
    delete_alarm,
    get_pending_alarms,
    mark_alarm_fired,
    save_alarm,
    save_message,
)
from felt_core.her_line import her_line
from felt_core.notify import notify

_LATE_S = 120  # past this, felt-core was down at alarm time: she says it's late


def plain_alarm(label: str) -> str:
    """Pure: the line when felt-llm can't word it."""
    return f"Alarm: {label}" if label else "Alarm"


def alarm_facts(label: str, fire_at: datetime, now: datetime) -> str:
    """Pure: what her model gets for an alarm going off."""
    what = (
        f"Mario's alarm is going off now, to remind him: \"{label}\" - say what it's for."
        if label
        else "Mario's alarm is going off now."
    )
    if (now - fire_at).total_seconds() > _LATE_S:
        what += f" It was set for {fire_at:%H:%M} but you were offline then, so it's late."
    return what


logger = logging.getLogger(__name__)

# Keyed by alarm id, so cancel_alarm can actually stop the sleeping task —
# without this, deleting the DB row wouldn't cancel a real future
# notification, only hide it from get_pending_alarms while it's still
# scheduled to fire on time regardless.
_scheduled_tasks: dict[int, asyncio.Task] = {}


async def _fire_at(config: FeltConfig, alarm_id: int, fire_at: datetime, label: str) -> None:
    delay = (fire_at - datetime.now()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    else:
        logger.info(
            "alarm %s was due at %s while felt-core wasn't running; firing now, late",
            alarm_id,
            fire_at,
        )
    with llm_gate.timely("alarm"):
        text = await her_line(
            config, alarm_facts(label, fire_at, datetime.now()), plain_alarm(label)
        )
    save_message(
        config.db_path, APP_SESSION, "assistant", text, kind=ALARM, source_ref=f"alarm:{alarm_id}"
    )
    await notify(config, text, title="Felt alarm")
    mark_alarm_fired(config.db_path, alarm_id)
    _scheduled_tasks.pop(alarm_id, None)


def schedule_alarm(config: FeltConfig, fire_at: datetime, label: str) -> int:
    alarm_id = save_alarm(config.db_path, fire_at.isoformat(), label)
    _scheduled_tasks[alarm_id] = asyncio.create_task(_fire_at(config, alarm_id, fire_at, label))
    return alarm_id


def cancel_alarm(config: FeltConfig, alarm_id: int) -> bool:
    """True if a pending alarm with this id was actually cancelled. Stops
    the sleeping task first (see _scheduled_tasks above) — deleting only
    the DB row would leave it scheduled to fire anyway."""
    task = _scheduled_tasks.pop(alarm_id, None)
    if task is not None:
        task.cancel()
    delete_alarm(config.db_path, alarm_id)
    return task is not None


def reschedule_pending_alarms(config: FeltConfig) -> None:
    """Called once at startup. A pending alarm from before a restart
    doesn't just vanish: one still in the future gets a fresh asyncio
    task at its original time; one whose time already passed while
    felt-core was down fires immediately (late, but not silently
    dropped — the alternative of only ever firing on-time alarms would
    quietly lose exactly the overnight "wake me up at 7am" case this
    exists for, if felt-core happened to restart at 6am)."""
    for alarm in get_pending_alarms(config.db_path):
        fire_at = datetime.fromisoformat(alarm["fire_at"])
        _scheduled_tasks[alarm["id"]] = asyncio.create_task(
            _fire_at(config, alarm["id"], fire_at, alarm["label"])
        )
