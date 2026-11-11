"""WP-44: the alert catalogue as rules - one rule per kind of alert.

What each kind of alert on the clockmost ntfy topic is, how loudly it
notifies, who handles it, and how long a self-fixing one waits for its
RESOLVED before it counts.

Built from the 1,449 real alerts (docs/plans/impl/alert-catalogue.md) and
gone through with Mario 10-03. Fixed rules, no model: her 12B only words
the line later (step 2), it never decides what an alert is.
"""

import re

from pydantic import BaseModel, ConfigDict

# urgent = loud notification, high = normal, default = in Systems quietly,
# low = history only (WP-42 8.5)
TIERS = ("urgent", "high", "default", "low")
# felt = she tells him and that's it (expected, or nothing to handle);
# claude = concerning, handed to Claude (WP-58); drop = shouldn't be sent
HANDLERS = ("felt", "claude", "drop")


class AlertType(BaseModel):
    """One kind of alert: its title pattern, tier, handler, and incident rules."""

    model_config = ConfigDict(frozen=True)

    name: str
    pattern: str  # searched in the alert title
    tier: str
    handler: str
    # expected events: told, never acted on (Mario 10-03)
    expected: bool = False
    # self-fixing ones: seconds to wait for the RESOLVED before it counts
    wait_s: int = 0
    # repeats of the same alert within this window fold into one incident
    fold_s: int = 6 * 3600
    # an alert that only ever follows another one joins that incident
    # instead of standing alone (updates SKIPPED follows a failed backup)
    joins: str | None = None
    # alerts whose title starts with what this matches are one problem
    # (Wazuh: every file a deploy touches is its own alert, same host)
    group: str | None = None


_M = 60

# Order matters: the first match wins.
CATALOGUE: tuple[AlertType, ...] = (
    # security -> Claude
    AlertType(name="wazuh-test", pattern=r"^Wazuh .*TEST", tier="low", handler="drop"),
    AlertType(name="wazuh-oom", pattern=r"^Wazuh .*running out of memory", tier="default", handler="felt"),
    AlertType(
        name="wazuh-fim",
        pattern=r"^Wazuh ",
        tier="high",
        handler="claude",
        fold_s=30 * _M,
        group=r"^Wazuh L\d+ \[[^\]]+\]",
    ),
    AlertType(name="honeypot", pattern=r"^HONEYPOT", tier="urgent", handler="claude"),
    AlertType(
        name="authentik-bruteforce", pattern=r"authentication-flow execution spike", tier="high", handler="claude"
    ),
    AlertType(name="crowdsec", pattern=r"CrowdSec", tier="high", handler="claude", wait_s=10 * _M),
    AlertType(name="trivy", pattern=r"^Trivy", tier="default", handler="claude"),
    # backups and updates -> Claude handles, he's told
    AlertType(
        name="updates-skipped", pattern=r"updates SKIPPED", tier="urgent", handler="claude", joins="backup-failed"
    ),
    AlertType(name="backup-failed", pattern=r"^backup .*FAILED", tier="urgent", handler="claude"),
    AlertType(name="storage-check", pattern=r"storage/backup check", tier="urgent", handler="claude"),
    AlertType(
        name="update-reverted", pattern=r"update reverted", tier="high", handler="claude", joins="update-reverting"
    ),
    AlertType(name="update-reverting", pattern=r"auto-reverting an update", tier="high", handler="claude"),
    AlertType(name="ci-failed", pattern=r"CI failed|deploy pipeline failed", tier="high", handler="claude"),
    # expected events -> told, not acted on
    AlertType(
        name="update-deployed",
        pattern=r"update deployed",
        tier="default",
        handler="felt",
        expected=True,
        fold_s=60 * _M,
    ),
    AlertType(name="pre-update-backup", pattern=r"pre-update backup", tier="low", handler="felt", expected=True),
    AlertType(name="weekly-reboot", pattern=r"weekly reboot", tier="default", handler="felt", expected=True),
    AlertType(name="ntfy-upgraded", pattern=r"ntfy upgraded", tier="default", handler="felt", expected=True),
    AlertType(
        name="container-version",
        pattern=r"Docker: Container .*Version has changed",
        tier="default",
        handler="felt",
        expected=True,
    ),
    AlertType(name="host-restarted", pattern=r"has been restarted", tier="default", handler="felt", expected=True),
    # dnf's own updates change the package count
    AlertType(
        name="packages-changed", pattern=r"Number of installed packages", tier="low", handler="felt", expected=True
    ),
    # the desktop's agent goes quiet whenever the PC is off
    AlertType(
        name="pc-agent",
        pattern=r"Windows: (Active checks are not available|Zabbix agent is not available)",
        tier="low",
        handler="felt",
        expected=True,
    ),
    # outages that mostly fix themselves -> wait, then Claude
    AlertType(
        name="authentik-outpost",
        pattern=r"Authentik: (embedded outpost|no worker)",
        tier="high",
        handler="claude",
        wait_s=15 * _M,
    ),
    AlertType(name="caddy-upstream", pattern=r"Caddy: an upstream", tier="high", handler="claude", wait_s=10 * _M),
    AlertType(name="route-check", pattern=r"route check failing", tier="high", handler="claude", wait_s=10 * _M),
    AlertType(name="container-down", pattern=r"Docker: Container ", tier="high", handler="claude", wait_s=15 * _M),
    AlertType(name="pihole-dns", pattern=r"Pi-hole", tier="high", handler="claude", wait_s=10 * _M),
    AlertType(name="home-assistant-vm", pattern=r"Home Assistant VM", tier="high", handler="claude", wait_s=10 * _M),
    # the rest: she reads and tells
    AlertType(
        name="zabbix-agent",
        pattern=r"Active checks are not available|Zabbix agent is not available",
        tier="low",
        handler="felt",
        wait_s=30 * _M,
    ),
    AlertType(
        name="windows-crash", pattern=r"unexpected shutdown|Critical-level System log", tier="high", handler="felt"
    ),
    AlertType(name="disk-space", pattern=r"FS \[.*Space is", tier="high", handler="felt", wait_s=15 * _M),
    AlertType(name="nitro-ollama", pattern=r"Ollama service FAILED", tier="default", handler="felt"),
    AlertType(name="nitro-gpu-hot", pattern=r"GPU overheating", tier="default", handler="felt"),
    AlertType(name="smart", pattern=r"SMART:", tier="default", handler="felt", wait_s=15 * _M),
    AlertType(name="health-check", pattern=r"health check: ", tier="default", handler="felt"),
    AlertType(
        name="router-network",
        pattern=r"Network Generic Device|Interface Intel",
        tier="low",
        handler="felt",
        wait_s=15 * _M,
    ),
    AlertType(
        name="host-load",
        pattern=r"(Windows|Linux): (The Memory Pages|High memory|High CPU|CPU privileged|"
        r"Load average|High swap|nvme)",
        tier="default",
        handler="felt",
        wait_s=15 * _M,
    ),
    AlertType(
        name="windows-service", pattern=r'Windows: ".*is not running', tier="low", handler="drop", wait_s=15 * _M
    ),
)

# Anything not in the catalogue: a new kind of alert is worth a look.
UNKNOWN = AlertType(name="unknown", pattern="", tier="default", handler="claude")

_BY_NAME = {t.name: t for t in CATALOGUE}
# Zabbix: "clockmost High: <subject>" ... "clockmost RESOLVED: <subject>"
_ZABBIX = re.compile(r"^clockmost (RESOLVED|Disaster|High|Average|Warning|Information|Not classified): (.+)$")


def by_name(name: str) -> AlertType:
    """The catalogue entry with this name, or UNKNOWN when it isn't listed."""
    return _BY_NAME.get(name, UNKNOWN)


def classify(title: str | None) -> AlertType:
    """The first catalogue entry whose pattern matches the alert title."""
    text = title or ""
    for alert_type in CATALOGUE:
        if re.search(alert_type.pattern, text):
            return alert_type
    return UNKNOWN


class Parsed(BaseModel):
    """One parsed alert title: its type, its matching key, and its RESOLVED flag."""

    model_config = ConfigDict(frozen=True)

    type: AlertType
    key: str  # what a repeat or a RESOLVED is matched on
    resolves: bool  # a RESOLVED, closing the alert it answers


def parse(title: str | None) -> Parsed:
    """The alert's type, its matching key, and whether it's a RESOLVED.

    Zabbix's severity can change between firings (Warning, then High), so
    the key is the subject without it.
    """
    text = (title or "").strip()
    match = _ZABBIX.match(text)
    subject = match.group(2) if match else text
    alert_type = classify(subject)
    grouped = re.match(alert_type.group, subject) if alert_type.group else None
    key = grouped.group(0) if grouped else subject
    return Parsed(type=alert_type, key=key, resolves=bool(match and match.group(1) == "RESOLVED"))
