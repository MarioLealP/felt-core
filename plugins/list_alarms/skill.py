"""Lists alarms: the phone's next one (the HA app's Next alarm sensor,
2026-10-18 - set_alarm puts them in the phone's Clock app since then) and
any still pending in Felt's own scheduling (the fallback when the phone
was unreachable, or a date further out than a phone alarm can hold)."""

from datetime import datetime

from felt_core.db import get_pending_alarms
from felt_core.ha_calendar import TZ
from felt_core.phone import PhoneUnavailable, next_alarm
from felt_core.plugin import SkillResult

name = "list_alarms"
description = "Says when Mario's next alarm is (on his phone) and any other alarms that are set."
parameters_schema = {"type": "object", "properties": {}}


def _when(at: datetime) -> str:
    return at.strftime("%A %I:%M %p").replace(" 0", " ")


async def handle(params, ctx):
    del params
    lines = []
    try:
        on_phone = await next_alarm(ctx.config)
        if on_phone is not None:
            lines.append(f"next on your phone: {_when(on_phone.astimezone(TZ))}")
        else:
            lines.append("none on your phone")
    except PhoneUnavailable:
        lines.append("couldn't reach your phone")

    for alarm in get_pending_alarms(ctx.config.db_path):
        suffix = f" ({alarm['label']})" if alarm["label"] else ""
        lines.append(f"{_when(datetime.fromisoformat(alarm['fire_at']))}{suffix} (Felt's own)")
    return SkillResult(text="Alarms: " + "; ".join(lines))
