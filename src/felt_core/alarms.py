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

from felt_core.config import FeltConfig
from felt_core.db import get_pending_alarms, mark_alarm_fired, save_alarm
from felt_core.notify import notify

logger = logging.getLogger(__name__)


async def _fire_at(config: FeltConfig, alarm_id: int, fire_at: datetime, label: str) -> None:
    delay = (fire_at - datetime.now()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    else:
        logger.info("alarm %s was due at %s while felt-core wasn't running; firing now, late", alarm_id, fire_at)
    text = f"Alarm: {label}" if label else "Alarm"
    await notify(config, text, title="Felt alarm")
    mark_alarm_fired(config.db_path, alarm_id)


def schedule_alarm(config: FeltConfig, fire_at: datetime, label: str) -> None:
    alarm_id = save_alarm(config.db_path, fire_at.isoformat(), label)
    asyncio.create_task(_fire_at(config, alarm_id, fire_at, label))


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
        asyncio.create_task(_fire_at(config, alarm["id"], fire_at, alarm["label"]))
