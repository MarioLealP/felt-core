"""Lets Felt be asked about email conversationally ("any new mail?", "what
was that Patreon email about?"), reading the mails Home Assistant forwarded
(emails.py) - the newest few that weren't noise, with the start of each
body so she can say what one is about."""

from felt_core import emails
from felt_core.plugin import SkillResult

name = "check_email"
description = (
    "Recently arrived emails in Mario's inbox (sender, subject, the start of the text). "
    "For any question about new mail or about an email she mentioned."
)
parameters_schema = {"type": "object", "properties": {}}

_EXCERPT = 400


async def handle(params, ctx):
    mails = emails.recent(ctx.config.db_path, limit=5)
    if not mails:
        return SkillResult(text="No recent emails.")
    lines = [
        f"From {m['sender']}: {m['subject']}\n  {m['body'][:_EXCERPT]}".rstrip() for m in mails
    ]
    return SkillResult(text="\n".join(lines))
