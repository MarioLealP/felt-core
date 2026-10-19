"""Reference skill, proving the plugin-directory mechanism end-to-end.

Delete this folder and get_time disappears from the brain's tool list —
no core code changes needed.
"""

from datetime import datetime

from felt_core.plugin import SkillResult

name = "get_time"
description = "Current local date and time."
parameters_schema = {"type": "object", "properties": {}}


async def handle(params, ctx):
    del ctx  # not needed for this skill
    return SkillResult(text=datetime.now().isoformat(timespec="seconds"))
