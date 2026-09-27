"""Proactive email awareness (assistant-skills.md, Mario 2026-09-11:
"on email arrives -> You have mail from X, want me summarize?").

Polls IMAP rather than using IDLE — Mario's own spec explicitly allows
either, and polling avoids the extra protocol handling IDLE needs (stdlib
`imaplib` has no built-in IDLE support). Runs as a background task for
the lifetime of the service, same shape as `ntfy_listener.py`: reconnect
on any error rather than dying.

`imaplib`/`email` are both blocking, synchronous stdlib modules — every
IMAP call here goes through `asyncio.to_thread` so it doesn't stall the
event loop.
"""

import asyncio
import email
import email.header
import email.utils
import imaplib
import logging

from felt_core.config import FeltConfig
from felt_core.db import is_known_email_uid, save_pending_email
from felt_core.notify import notify

logger = logging.getLogger(__name__)


def _decode_header(raw: str | None) -> str:
    if not raw:
        return "(no subject)"
    parts = email.header.decode_header(raw)
    return "".join(
        part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part
        for part, charset in parts
    )


def _sender_display_name(raw_from: str | None) -> str:
    """Prefer the friendly name ("Jane Doe") over the raw address when
    one is present — that's what actually reads naturally in "You have
    mail from X.\""""
    if not raw_from:
        return "(unknown sender)"
    name, address = email.utils.parseaddr(raw_from)
    return _decode_header(name) if name else address


def _require_configured(name: str, value: str | None) -> str:
    """`watch()` already checks `imap_host`/`imap_user` before starting the
    background loop, but `_connect` is also reachable directly from
    `fetch_email_body` (the check_email/summarize path) without going
    through that guard - narrow here too rather than handing `imaplib` a
    `None` and letting it fail with an unrelated low-level error."""
    if not value:
        raise RuntimeError(f"IMAP {name} is not configured")
    return value


def _connect(config: FeltConfig) -> imaplib.IMAP4_SSL:
    host = _require_configured("host", config.imap_host)
    user = _require_configured("user", config.imap_user)
    password = _require_configured("password", config.imap_password)
    conn = imaplib.IMAP4_SSL(host, config.imap_port)
    conn.login(user, password)
    conn.select(config.imap_mailbox)
    return conn


def _fetch_unseen(config: FeltConfig) -> list[dict]:
    """Blocking: connect, find UNSEEN messages, pull just the headers
    needed for the proactive ping (not the body — that's only fetched
    later, on confirmation, to avoid parsing every message's full MIME
    structure for ones Mario never asks about)."""
    conn = _connect(config)
    try:
        # IMAP UID commands, not plain sequence-number commands: sequence
        # numbers are transient and shift down whenever an older message
        # in the mailbox is deleted, which would silently corrupt the
        # uid-based dedup in pending_emails (2026-09-14 review finding).
        # imaplib's own docs use `M.uid("search", None, "ALL")` - None means "no CHARSET",
        # the documented idiom for UID SEARCH; the imaplib stub types uid()'s *args as str
        # only, a stub gap, not a bug.
        status, data = conn.uid("SEARCH", None, "UNSEEN")  # ty: ignore[invalid-argument-type]
        if status != "OK":
            return []
        uids = data[0].split()
        found = []
        for uid in uids:
            uid_str = uid.decode()
            status, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw_headers = msg_data[0][1]
            msg = email.message_from_bytes(raw_headers)
            found.append(
                {
                    "uid": uid_str,
                    "sender": _sender_display_name(msg.get("From")),
                    "subject": _decode_header(msg.get("Subject")),
                }
            )
        return found
    finally:
        conn.logout()


def fetch_email_body(config: FeltConfig, uid: str) -> str:
    """Blocking: the full plain-text body for one message, by UID — used
    only when Mario actually confirms he wants a summary."""
    conn = _connect(config)
    try:
        status, msg_data = conn.uid("FETCH", uid, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            return ""
        msg = email.message_from_bytes(msg_data[0][1])
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    raw = part.get_payload(decode=True)
                    if isinstance(raw, bytes):
                        charset = part.get_content_charset() or "utf-8"
                        return raw.decode(charset, errors="replace")
            return "(no plain-text body found)"
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        return payload.decode(charset, errors="replace") if isinstance(payload, bytes) else ""
    finally:
        conn.logout()


async def _poll_once(config: FeltConfig) -> None:
    for found in await asyncio.to_thread(_fetch_unseen, config):
        if is_known_email_uid(config.db_path, found["uid"]):
            continue
        save_pending_email(config.db_path, found["uid"], found["sender"], found["subject"])
        await notify(
            config,
            f"You have mail from {found['sender']}: {found['subject']}. Want me to summarize it?",
            title="New email",
        )
        logger.info("new mail: %s / %s", found["sender"], found["subject"])


async def watch(config: FeltConfig) -> None:
    if not config.imap_host or not config.imap_user:
        logger.info("IMAP not configured, email watcher not starting")
        return

    while True:
        try:
            await _poll_once(config)
        except Exception:
            logger.exception("email watcher error, retrying after the normal interval")
        await asyncio.sleep(config.imap_poll_seconds)
