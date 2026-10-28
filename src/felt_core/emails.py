"""Her "btw, you've got mail" (WP-43, cm-0h6, 2026-10-27).

Mail comes from Home Assistant: its IMAP integration (Gmail, with an app
password HA holds - Felt has no Google credential) fires `imap_content` for
each new unread inbox mail, and Felt's own HA integration forwards it to
POST /v1/ha/email. Felt's HA user isn't an admin, and HA only lets admins
listen for that event.

Gmail's tabs (category:primary / promotions) match nothing over IMAP, so the
Promotions/Social skip happens here instead (Mario 2026-10-27, "yeah all 3"):

- a sender on the noise list ("I don't care about X emails",
  mute_email_sender) is never mentioned;
- mailing-list mail (a List-Unsubscribe header) goes past her model first -
  Patreon's mail is list mail too, and that's Mario's own example of one
  worth hearing about ("hey btw have you checked that email from Patreon");
- anything else (a person wrote it) is always mentioned.

Each mail is mentioned once, at most one mention per `email_mention_gap_s`
(an hour); everything waiting by then goes into that one line, which she
words herself. "Yes" / "tell me more" right after it summarises from the
body HA sent along.

A row's `state`: new (not looked at yet) -> waiting (to be mentioned) |
noise (muted sender) | skipped (her model said no) | stale (waited past a
day) -> mentioned -> summarised.
"""

import asyncio
import html
import json
import logging
import re
import sqlite3
from email.utils import parseaddr

from felt_core import features, llm_gate, local_llm
from felt_core.config import FeltConfig
from felt_core.db import APP_SESSION, EMAIL, _connect, save_message

logger = logging.getLogger(__name__)

TICK_S = 60
# mentioned by name in the line; the rest are counted
NAMED = 3
STALE = "-1 day"  # SQLite datetime modifier
# "yes" / "tell me more" counts as answering her mention this long after it
ANSWER_WINDOW = "-10 minutes"
# the desktop may say it later (quiet / hidden hold it back), not much later
_TTL_S = 2 * 60 * 60
_BODY_CHARS = 6000
# two-label public suffixes, for muting a whole sender domain
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac"}


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS emails ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,"
        " sender TEXT NOT NULL, address TEXT NOT NULL, subject TEXT NOT NULL,"
        " body TEXT NOT NULL, bulk INTEGER NOT NULL DEFAULT 0,"
        " state TEXT NOT NULL DEFAULT 'new',"
        " received_at TEXT NOT NULL DEFAULT (datetime('now')), mentioned_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS email_noise ("
        " pattern TEXT PRIMARY KEY, added_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )


def _open(db_path: str) -> sqlite3.Connection:
    conn = _connect(db_path)
    _ensure(conn)
    return conn


def split_sender(raw: str) -> tuple[str, str]:
    """Pure: ("Patreon", "bingo@patreon.com") from 'Patreon <bingo@patreon.com>';
    the address doubles as the name when there is none."""
    name, address = parseaddr(raw)
    address = address.lower()
    return (name.strip() or address or raw.strip()), address


def plain_text(body: str) -> str:
    """Pure: an HTML-only mail's text, roughly - tags, styles and scripts out."""
    body = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html.unescape(body)).strip()


def domain_of(address: str) -> str:
    """Pure: the registered domain, roughly - "mail.patreon.com" -> "patreon.com",
    "news.loja.com.pt" -> "loja.com.pt"."""
    labels = address.rsplit("@", 1)[-1].split(".")
    keep = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def is_noise(address: str, patterns: list[str]) -> bool:
    """Pure: a pattern is a whole address, or a domain that covers its
    subdomains too."""
    host = address.rsplit("@", 1)[-1]
    for p in patterns:
        if "@" in p:
            if address == p:
                return True
        elif host == p or host.endswith("." + p):
            return True
    return False


def add(db_path: str, key: str, sender: str, subject: str, body: str, bulk: bool) -> int | None:
    """Store a mail HA forwarded; None when it was already here (HA repeats the
    newest one on every restart)."""
    name, address = split_sender(sender)
    conn = _open(db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO emails (key, sender, address, subject, body, bulk)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key, name, address, subject.strip(), plain_text(body), int(bulk)),
            )
    finally:
        conn.close()
    return cur.lastrowid if cur.rowcount else None


def _noise(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT pattern FROM email_noise")]


def _set_state(db_path: str, ids: list[int], state: str, mentioned: bool = False) -> None:
    if not ids:
        return
    conn = _open(db_path)
    try:
        with conn:
            stamp = ", mentioned_at = datetime('now')" if mentioned else ""
            conn.executemany(
                f"UPDATE emails SET state = ?{stamp} WHERE id = ?", [(state, i) for i in ids]
            )
    finally:
        conn.close()


# the reason first: a bare yes/no came out "no" for a bill, a security alert
# and a Patreon post in the first real-model run
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "mention": {"type": "boolean"}},
    "required": ["reason", "mention"],
}
_JUDGE = (
    "Mario got this automated email (a mailing list or a service):\n"
    "From: {sender}\nSubject: {subject}\n{body}\n\n"
    "Should you give him a heads-up about it? YES if it is any of: a post or news from "
    "a creator or project he supports (Patreon, Ko-fi, ...); a parcel or delivery; a "
    "bill, invoice or payment; a security alert or account problem; something that "
    "needs him to do anything. NO for ads, sales and discounts, digests and "
    'newsletters, social-network notifications. In "reason" say in a few words which '
    'it is, then "mention".'
)


async def _worth_mentioning(config: FeltConfig, row: tuple) -> bool:
    _, sender, address, subject, body = row
    with llm_gate.ambient("email"):
        reply = await local_llm.chat(
            [
                {
                    "role": "user",
                    "content": _JUDGE.format(
                        sender=f"{sender} <{address}>", subject=subject, body=body[:800]
                    ),
                }
            ],
            max_tokens=60,
            temperature=0,
            json_schema=JUDGE_SCHEMA,
            config=config,
        )
    try:
        return bool(json.loads(reply.text)["mention"])
    except (ValueError, KeyError, TypeError):
        return False


async def sort_new(config: FeltConfig) -> None:
    """Decide each new mail: noise, waiting, or (list mail) her call. A list
    mail she couldn't judge (felt-llm busy or down) stays new for the next tick."""
    conn = _open(config.db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE emails SET state = 'stale'"
                f" WHERE state IN ('new', 'waiting') AND received_at < datetime('now', '{STALE}')"
            )
        patterns = _noise(conn)
        rows = conn.execute(
            "SELECT id, sender, address, subject, body, bulk FROM emails"
            " WHERE state = 'new' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    # the ones needing no model call first: a list mail she can't judge yet
    # ends the round, and mustn't hold a person's mail back with it
    rows.sort(key=lambda r: bool(r[5]) and not is_noise(r[2], patterns))
    for row in rows:
        mail_id, bulk = row[0], row[5]
        if is_noise(row[2], patterns):
            _set_state(config.db_path, [mail_id], "noise")
        elif not bulk:
            _set_state(config.db_path, [mail_id], "waiting")
        else:
            try:
                keep = await _worth_mentioning(config, row[:5])
            except local_llm.LocalLLMUnavailable:
                return
            _set_state(config.db_path, [mail_id], "waiting" if keep else "skipped")


def mention_facts(mails: list[tuple[str, str]]) -> tuple[str, str]:
    """Pure: (facts for her line, the plain fallback) about the waiting mails,
    [(sender, subject)], people before lists."""
    named = [f'from {s}: "{subj}"' if subj else f"from {s}" for s, subj in mails[:NAMED]]
    more = len(mails) - NAMED
    listed = "; ".join(named) + (f"; and {more} more" if more > 0 else "")
    facts = (
        f"Mario has new email he hasn't heard about - {listed}. Mention it in passing, "
        'like a "btw", naming every sender, and that you can tell him more.'
    )
    fallback = f"Btw, you've got mail {named[0]}" + (
        f" and {len(mails) - 1} more." if len(mails) > 1 else "."
    )
    return facts, fallback


def _since_last_mention_s(conn: sqlite3.Connection) -> float | None:
    row = conn.execute(
        "SELECT (julianday('now') - julianday(MAX(mentioned_at))) * 86400 FROM emails"
    ).fetchone()
    return row[0] if row and row[0] is not None else None


async def mention(config: FeltConfig) -> str | None:
    """Her one line about everything waiting, if the hour since the last one is
    up. Returns the line said."""
    from felt_core.asides import schedule_pick
    from felt_core.her_line import her_line
    from felt_core.proactive import enqueue_proactive

    conn = _open(config.db_path)
    try:
        since = _since_last_mention_s(conn)
        if since is not None and since < config.email_mention_gap_s:
            return None
        rows = conn.execute(
            "SELECT id, sender, subject FROM emails WHERE state = 'waiting' ORDER BY bulk, id"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    facts, fallback = mention_facts([(r[1], r[2]) for r in rows])
    with llm_gate.timely("email"):
        line = await her_line(config, facts, fallback)
    ids = [r[0] for r in rows]
    _set_state(config.db_path, ids, "mentioned", mentioned=True)
    # her log (Margins, asides, her history) under its own kind; the desktop
    # just says it
    save_message(
        config.db_path,
        APP_SESSION,
        "assistant",
        line,
        kind=EMAIL,
        source_ref="email:" + ",".join(map(str, ids)),
    )
    enqueue_proactive(config, "email", line, ttl_s=_TTL_S, record=False)
    schedule_pick(config, APP_SESSION)
    logger.info("emails: mentioned %s -> %r", ids, line)
    return line


async def tick(config: FeltConfig) -> None:
    if not features.enabled(config.db_path, "email.new_mail"):
        return
    await sort_new(config)
    await mention(config)


async def watch(config: FeltConfig) -> None:
    while True:
        try:
            await tick(config)
        except Exception:
            logger.exception("emails: tick failed")
        await asyncio.sleep(TICK_S)


def just_mentioned(db_path: str) -> list[dict]:
    """The mails of her last mention, if it was within ANSWER_WINDOW and he
    hasn't had them summarised: what a bare "yes" / "tell me more" means."""
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT id, sender, subject, body FROM emails WHERE state = 'mentioned'"
            f" AND mentioned_at >= datetime('now', '{ANSWER_WINDOW}')"
            " AND mentioned_at = (SELECT MAX(mentioned_at) FROM emails)"
            " ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "sender": r[1], "subject": r[2], "body": r[3]} for r in rows]


def mark_summarised(db_path: str, ids: list[int]) -> None:
    _set_state(db_path, ids, "summarised")


def summary_prompt(mails: list[dict]) -> str:
    """Pure: what she's asked to summarise."""
    per = _BODY_CHARS // max(len(mails), 1)
    parts = [f"From {m['sender']} (subject: {m['subject']}):\n{m['body'][:per]}" for m in mails]
    which = "this email" if len(mails) == 1 else "these emails, one or two sentences each"
    return (
        f"Summarize {which} conversationally, as if telling a friend what it says:\n\n"
        + "\n\n".join(parts)
    )


def recent(db_path: str, limit: int = 5) -> list[dict]:
    """The newest mails that weren't noise, for "any new mail?"."""
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT sender, subject, body, state FROM emails WHERE state != 'noise'"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [{"sender": r[0], "subject": r[1], "body": r[2], "state": r[3]} for r in rows]


def noise_pattern(who: str, senders: list[tuple[str, str, bool]]) -> str | None:
    """Pure: what muting `who` adds to the noise list. An address or a domain
    is taken as said. A name is looked up in the recent senders [(name,
    address, bulk)], newest first: a list sender's whole domain goes (Patreon
    mails from several addresses), a person's one address (never all of
    gmail.com)."""
    who = who.strip().lower()
    if not who:
        return None
    if "@" in who:
        return who
    if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", who):
        return who
    for name, address, bulk in senders:
        if who in name.lower() or who in address:
            return domain_of(address) if bulk else address
    return None


def mute(db_path: str, who: str) -> str | None:
    """ "I don't care about X emails": the pattern added (None = no sender like
    that); mails from it still waiting won't be mentioned either."""
    conn = _open(db_path)
    try:
        senders = [
            (r[0], r[1], bool(r[2]))
            for r in conn.execute(
                "SELECT sender, address, bulk FROM emails ORDER BY id DESC LIMIT 100"
            )
        ]
        pattern = noise_pattern(who, senders)
        if pattern is None:
            return None
        with conn:
            conn.execute("INSERT OR IGNORE INTO email_noise (pattern) VALUES (?)", (pattern,))
            waiting = conn.execute(
                "SELECT id, address FROM emails WHERE state IN ('new', 'waiting')"
            ).fetchall()
            conn.executemany(
                "UPDATE emails SET state = 'noise' WHERE id = ?",
                [(i,) for i, a in waiting if is_noise(a, [pattern])],
            )
    finally:
        conn.close()
    return pattern
