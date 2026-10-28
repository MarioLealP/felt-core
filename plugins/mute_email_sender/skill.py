"""WP-43: "I don't care about X emails" - X goes on the noise list and she
never mentions its mail again (emails.py; a mailing list's whole domain, a
person's one address)."""

from felt_core import emails
from felt_core.plugin import SkillResult

name = "mute_email_sender"
description = (
    "Stops her mentioning emails from a sender for good - when Mario says he doesn't "
    "care about someone's emails."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "sender": {
            "type": "string",
            "description": "Who, as he said it: a name, an address or a domain",
        }
    },
    "required": ["sender"],
}


async def handle(params, ctx):
    who = params.get("sender") or ""
    pattern = emails.mute(ctx.config.db_path, who)
    if pattern is None:
        return SkillResult(text=f"No recent email from anyone like {who!r} - nothing muted.")
    return SkillResult(text=f"Muted: emails from {pattern} won't be mentioned again.")
