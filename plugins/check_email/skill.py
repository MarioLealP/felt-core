"""Lets Felt be asked about email conversationally ("any new mail?"),
reading the local log the proactive email_watcher.py already keeps —
the read half of the email integration, same pattern as
get_recent_alerts is for ntfy."""

from felt_core.db import get_recent_pending_emails
from felt_core.plugin import SkillResult

name = "check_email"
description = (
    "Checks Felt's own locally-stored log of recently-arrived emails "
    "(sender + subject only, from the proactive IMAP watcher). Use this "
    "for any question about new mail/email — NOT delegate_research or "
    "web_search, this is local data about Mario's own inbox."
)
parameters_schema = {"type": "object", "properties": {}}


async def handle(params, ctx):
    emails = get_recent_pending_emails(ctx.config.db_path, limit=5)
    if not emails:
        return SkillResult(text="No recent emails logged.")
    lines = [f"From {e['sender']}: {e['subject']}" for e in emails]
    return SkillResult(text="\n".join(lines))
