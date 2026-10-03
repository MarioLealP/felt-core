"""WP-44 step 2: her line about an incident.

For every alert she tells Mario three things (Mario 10-03): is it expected,
is there anything he can do, who's on it. Those facts are fixed - the
catalogue (alert_types.py) and the incident decide them, and MEANINGS below
says in plain words what each type is and what, if anything, he can do. Her
12B only words it in her voice; it is never asked to work out what an alert
means, so it can't invent a cause or a task.

When: an incident that counts (open) gets a line; an expected one (noted)
gets one once its run has settled, so a burst of deploys is one line; one
that counted and then came back gets a short "it's back" line; one Claude
looked into (WP-58, responder.py) gets a follow-up with his outcome - or
with "Claude couldn't look", so nothing is dropped quietly. Lines are
stored on the incident and in her log as kind ALERT - never in the Margins
with her chatter (WP-42 2.8). Delivering them (notifications everywhere,
the Systems page) is 42.5.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from felt_core import local_llm
from felt_core.alert_types import by_name
from felt_core.config import FeltConfig
from felt_core.db import ALERT, APP_SESSION, _connect, save_message
from felt_core.incidents import _ensure

logger = logging.getLogger(__name__)

# plain words for each catalogue type: (what it is, what Mario can do - ""
# for nothing). Facts for her to word, never to improve on.
MEANINGS: dict[str, tuple[str, str]] = {
    "wazuh-test": ("a test message from the Wazuh setup", ""),
    "wazuh-oom": ("a machine ran out of memory and the system killed a process", ""),
    "wazuh-fim": ("Wazuh saw protected files or settings change on a machine", ""),
    "honeypot": ("something on the network touched the honeypot trap", ""),
    "authentik-bruteforce": (
        "a lot of login attempts on the SSO in a few minutes, either an attack or "
        "Mario logging in a lot",
        "",
    ),
    "crowdsec": ("CrowdSec, the server's firewall blocker, stopped enforcing", ""),
    "trivy": ("the weekly security scan of the server's containers", ""),
    "updates-skipped": ("the server skipped its updates because the backup before them failed", ""),
    "backup-failed": ("a server backup failed", ""),
    "storage-check": (
        "the server's storage check found a problem with the backup disk or backups",
        "if it says the backup disk isn't mounted, check it's plugged in and powered",
    ),
    "update-reverted": (
        "an update broke something and was rolled back",
        "if the details say a Renovate PR stays open for his review, review it",
    ),
    "update-reverting": (
        "an update broke something and is being rolled back",
        "if the details say a Renovate PR stays open for his review, review it",
    ),
    "ci-failed": ("a build or deploy of one of the apps failed", ""),
    "update-deployed": ("apps on the server were updated and checked healthy", ""),
    "pre-update-backup": ("the backup before updates ran fine", ""),
    "weekly-reboot": ("the server's planned Monday reboot", ""),
    "ntfy-upgraded": ("the notification service was upgraded", ""),
    "container-version": ("an app on the server changed version", ""),
    "host-restarted": ("a machine restarted", ""),
    "authentik-outpost": (
        "the SSO login gate is down, so apps behind it can't be reached",
        "",
    ),
    "caddy-upstream": ("the web server can't reach one of the apps behind it", ""),
    "route-check": ("one of the server's websites stopped answering", ""),
    "container-down": ("an app on the server is unhealthy or stopped", ""),
    "pihole-dns": ("Pi-hole, the home network's DNS, stopped resolving names", ""),
    "home-assistant-vm": ("the Home Assistant VM isn't running", ""),
    "zabbix-agent": ("monitoring lost contact with a Linux machine", ""),
    "windows-crash": ("the Windows desktop went down hard (power loss or crash)", ""),
    "disk-space": ("a disk is running out of space", "free some space on it"),
    "nitro-ollama": ("the AI service on the Nitro5 laptop (the on-demand GPU node) failed", ""),
    "nitro-gpu-hot": ("the Nitro5 laptop's GPU is running too hot", ""),
    "packages-changed": ("the set of installed packages on a machine changed (updates)", ""),
    "pc-agent": ("monitoring lost contact with Mario's Windows PC, because it's switched off", ""),
    "smart": ("a disk's health sensor reported a problem (temperature or errors)", ""),
    "health-check": ("the server's health check found something off", ""),
    "router-network": ("the router saw unusual network traffic", ""),
    "host-load": ("a machine is under heavy load (CPU, memory or disk)", ""),
    "windows-service": ("a Windows service isn't running", ""),
    "unknown": ("an alert of a kind not seen before", ""),
}

# an expected run (deploys) has settled when nothing joined it for this long
SETTLE = timedelta(minutes=10)
MAX_WORDS = 40
_MESSAGE_CHARS = 400

_INSTRUCTION = (
    "A server alert just came in and you're the one telling Mario about it. Write ONE "
    "short message (max ~35 words), in character, casual, like a text. It must get "
    "across, in your own words: what happened; whether it's expected; whether there's "
    "anything for him to do; and who's on it. Use ONLY the facts below - don't guess "
    "causes, don't invent steps, don't add numbers, names or files that aren't there. "
    "Plain words, no jargon unless it's in the facts. No emojis."
)
_FOLLOWUP = (
    "Earlier you told Mario that Claude was looking into a server alert. Claude has "
    "reported back. Write ONE short message (max ~35 words), in character, casual, "
    "telling Mario the outcome in your own words: what it turned out to be, and "
    "whether there's anything for him to do. Use ONLY the facts below - don't add "
    "causes, steps, numbers, names or files that aren't there. No emojis."
)
_MISSED = (
    "Earlier you told Mario that Claude was looking into a server alert, but Claude "
    "couldn't. Write ONE short message (max ~30 words), in character, telling him so "
    "and that he may want to look himself. Use ONLY the facts below. No emojis."
)
_VERDICTS = {
    "expected": "it was expected - something on our side",
    "fixed-itself": "it fixed itself",
    "needs-mario": "it needs Mario",
    "unknown": "Claude couldn't tell for sure",
}
_BACK = (
    "A server problem you told Mario about earlier is fixed now. Write ONE very short "
    "message (max ~15 words), in character, saying it's back to normal. Use ONLY the "
    "facts below."
)


def facts(incident: dict) -> str:
    """The fixed facts her line has to carry."""
    alert_type = by_name(incident["type"])
    about, todo = MEANINGS.get(incident["type"], MEANINGS["unknown"])
    if alert_type.expected:
        expected = "yes, this is meant to happen"
    elif incident["state"] == "open" and alert_type.wait_s:
        expected = f"no - it didn't fix itself within {alert_type.wait_s // 60} minutes"
    else:
        expected = "no"
    if alert_type.expected:
        whose = "nobody needs to, it's just so he knows"
    elif alert_type.handler == "claude":
        whose = "Claude is looking into it"
    else:
        whose = "you (Felt) are keeping an eye on it"
    lines = [
        f"What it is: {about}.",
        f"The alert: {incident['title']}",
    ]
    detail = " / ".join(
        (a.get("message") or "").strip().replace("\n", " / ")
        for a in incident.get("alerts", [])
        if (a.get("message") or "").strip()
    )
    if detail:
        lines.append(f"Details: {detail[:_MESSAGE_CHARS]}")
    if incident["count"] > 1:
        lines.append(f"It fired {incident['count']} times.")
    lines += [
        f"Expected: {expected}.",
        f"Anything for Mario to do: {todo or 'nothing'}.",
        f"Who's on it: {whose}.",
    ]
    return "\n".join(lines)


def followup_facts(incident: dict) -> str:
    """The facts of her follow-up: Claude's outcome, or that he couldn't look."""
    about, _ = MEANINGS.get(incident["type"], MEANINGS["unknown"])
    lines = [f"What the alert was about: {about}.", f"The alert: {incident['title']}"]
    if incident.get("report"):
        report = json.loads(incident["report"])
        verdict = _VERDICTS.get(report.get("verdict", ""), _VERDICTS["unknown"])
        todo = str(report.get("for_mario") or "nothing")
        lines += [
            f"What Claude concluded: {verdict}.",
            f"What Claude found: {str(report.get('summary', ''))[:_MESSAGE_CHARS]}",
            f"Anything for Mario to do: {todo[:_MESSAGE_CHARS]}.",
        ]
        if report.get("verdict") in ("needs-mario", "unknown"):
            lines.append("He can join Claude's investigation to go through it together.")
    elif incident.get("handed_at"):
        lines.append("Claude started looking but didn't finish in time.")
    else:
        lines.append("Claude didn't get to it (he's offline or today's limit is used up).")
    if not incident.get("report"):
        lines.append("Anything for Mario to do: have a look himself, or ask Claude directly.")
    return "\n".join(lines)


def _clip(text: str) -> str:
    words = text.strip(" \n\"'").split()
    return " ".join(words[:MAX_WORDS])


async def line_for(config: FeltConfig, incident: dict, kind: str = "line") -> str:
    from felt_core.brain.replies import local_reply

    if kind == "followup":
        instruction = _FOLLOWUP if incident.get("report") else _MISSED
        prompt = f"{instruction}\n\nFacts:\n{followup_facts(incident)}"
    else:
        prompt = f"{_BACK if kind == 'back' else _INSTRUCTION}\n\nFacts:\n{facts(incident)}"
    reply = await local_reply([{"role": "user", "content": prompt}], config, include_facts=False)
    return _clip(reply)


# -- which incidents are due a line ------------------------------------------


def due(db_path: str, now: datetime | None = None) -> list[tuple[int, str]]:
    """(incident id, which line) for every incident owed one: "line" for open
    ones and settled expected runs, "back" for ones that counted, got a line
    and have since resolved, "followup" once Claude reported or was missed
    (always after her first line)."""
    settled = ((now or datetime.now(UTC)) - SETTLE).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect(db_path)
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT id, 'line' FROM incidents WHERE line IS NULL AND (state = 'open'"
            " OR (state = 'noted' AND last_at <= ?))"
            " UNION ALL SELECT id, 'back' FROM incidents WHERE state = 'resolved'"
            " AND line IS NOT NULL AND back_line IS NULL"
            " UNION ALL SELECT id, 'followup' FROM incidents WHERE line IS NOT NULL"
            " AND followup IS NULL AND (report IS NOT NULL OR missed_at IS NOT NULL)"
            " ORDER BY 1",
            (settled,),
        ).fetchall()
    finally:
        conn.close()
    return [(i, kind) for i, kind in rows]


_COLUMNS = {"line": "line", "back": "back_line", "followup": "followup"}


def store(db_path: str, incident_id: int, text: str, kind: str = "line") -> None:
    conn = _connect(db_path)
    try:
        _ensure(conn)
        with conn:
            column = _COLUMNS[kind]
            conn.execute(
                f"UPDATE incidents SET {column} = ?, line_at = datetime('now') WHERE id = ?",
                (text, incident_id),
            )
    finally:
        conn.close()
    save_message(
        db_path,
        APP_SESSION,
        "assistant",
        text,
        kind=ALERT,
        source_ref=f"incident:{incident_id}{'' if kind == 'line' else ':' + kind}",
    )


async def write_due(config: FeltConfig) -> int:
    """Writes every line that's owed; stops at the first felt-llm outage
    (the next round retries). Returns how many it wrote."""
    from felt_core.incidents import get

    written = 0
    for incident_id, kind in due(config.db_path):
        incident = get(config.db_path, incident_id)
        if incident is None:
            continue
        try:
            text = await line_for(config, incident, kind)
        except local_llm.LocalLLMUnavailable:
            logger.info("alert lines: felt-llm unavailable, retrying next round")
            break
        if not text:
            continue
        store(config.db_path, incident_id, text, kind)
        logger.info("alert line for incident %d (%s): %r", incident_id, incident["type"], text)
        written += 1
    return written
