"""WP-44: the alert catalogue as rules - what each kind of alert on the
clockmost ntfy topic is, how loudly it notifies, who handles it, and how
long a self-fixing one waits for its RESOLVED before it counts.

Built from the 1,449 real alerts (docs/plans/impl/alert-catalogue.md) and
gone through with Mario 10-03. Fixed rules, no model: her 12B only words
the line later (step 2), it never decides what an alert is.
"""

import re
from dataclasses import dataclass

# urgent = loud notification, high = normal, default = in Systems quietly,
# low = history only (WP-42 8.5)
TIERS = ("urgent", "high", "default", "low")
# felt = she tells him and that's it (expected, or nothing to handle);
# claude = concerning, handed to Claude (WP-58); drop = shouldn't be sent
HANDLERS = ("felt", "claude", "drop")


@dataclass(frozen=True)
class AlertType:
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
    AlertType("wazuh-test", r"^Wazuh .*TEST", "low", "drop"),
    AlertType("wazuh-oom", r"^Wazuh .*running out of memory", "default", "felt"),
    AlertType(
        "wazuh-fim", r"^Wazuh ", "high", "claude", fold_s=30 * _M, group=r"^Wazuh L\d+ \[[^\]]+\]"
    ),
    AlertType("honeypot", r"^HONEYPOT", "urgent", "claude"),
    AlertType("authentik-bruteforce", r"authentication-flow execution spike", "high", "claude"),
    AlertType("crowdsec", r"CrowdSec", "high", "claude", wait_s=10 * _M),
    AlertType("trivy", r"^Trivy", "default", "claude"),
    # backups and updates -> Claude handles, he's told
    AlertType("updates-skipped", r"updates SKIPPED", "urgent", "claude", joins="backup-failed"),
    AlertType("backup-failed", r"^backup .*FAILED", "urgent", "claude"),
    AlertType("storage-check", r"storage/backup check", "urgent", "claude"),
    AlertType("update-reverted", r"update reverted", "high", "claude", joins="update-reverting"),
    AlertType("update-reverting", r"auto-reverting an update", "high", "claude"),
    AlertType("ci-failed", r"CI failed|deploy pipeline failed", "high", "claude"),
    # expected events -> told, not acted on
    AlertType(
        "update-deployed", r"update deployed", "default", "felt", expected=True, fold_s=60 * _M
    ),
    AlertType("pre-update-backup", r"pre-update backup", "low", "felt", expected=True),
    AlertType("weekly-reboot", r"weekly reboot", "default", "felt", expected=True),
    AlertType("ntfy-upgraded", r"ntfy upgraded", "default", "felt", expected=True),
    AlertType(
        "container-version",
        r"Docker: Container .*Version has changed",
        "default",
        "felt",
        expected=True,
    ),
    AlertType("host-restarted", r"has been restarted", "default", "felt", expected=True),
    # dnf's own updates change the package count
    AlertType("packages-changed", r"Number of installed packages", "low", "felt", expected=True),
    # the desktop's agent goes quiet whenever the PC is off
    AlertType(
        "pc-agent",
        r"Windows: (Active checks are not available|Zabbix agent is not available)",
        "low",
        "felt",
        expected=True,
    ),
    # outages that mostly fix themselves -> wait, then Claude
    AlertType(
        "authentik-outpost",
        r"Authentik: (embedded outpost|no worker)",
        "high",
        "claude",
        wait_s=15 * _M,
    ),
    AlertType("caddy-upstream", r"Caddy: an upstream", "high", "claude", wait_s=10 * _M),
    AlertType("route-check", r"route check failing", "high", "claude", wait_s=10 * _M),
    AlertType("container-down", r"Docker: Container ", "high", "claude", wait_s=15 * _M),
    AlertType("pihole-dns", r"Pi-hole", "high", "claude", wait_s=10 * _M),
    AlertType("home-assistant-vm", r"Home Assistant VM", "high", "claude", wait_s=10 * _M),
    # the rest: she reads and tells
    AlertType(
        "zabbix-agent",
        r"Active checks are not available|Zabbix agent is not available",
        "low",
        "felt",
        wait_s=30 * _M,
    ),
    AlertType("windows-crash", r"unexpected shutdown|Critical-level System log", "high", "felt"),
    AlertType("disk-space", r"FS \[.*Space is", "high", "felt", wait_s=15 * _M),
    AlertType("nitro-ollama", r"Ollama service FAILED", "default", "felt"),
    AlertType("nitro-gpu-hot", r"GPU overheating", "default", "felt"),
    AlertType("smart", r"SMART:", "default", "felt", wait_s=15 * _M),
    AlertType("health-check", r"health check: ", "default", "felt"),
    AlertType(
        "router-network", r"Network Generic Device|Interface Intel", "low", "felt", wait_s=15 * _M
    ),
    AlertType(
        "host-load",
        r"(Windows|Linux): (The Memory Pages|High memory|High CPU|CPU privileged|"
        r"Load average|High swap|nvme)",
        "default",
        "felt",
        wait_s=15 * _M,
    ),
    AlertType("windows-service", r'Windows: ".*is not running', "low", "drop", wait_s=15 * _M),
)

# Anything not in the catalogue: a new kind of alert is worth a look.
UNKNOWN = AlertType("unknown", "", "default", "claude")

_BY_NAME = {t.name: t for t in CATALOGUE}
# Zabbix: "clockmost High: <subject>" ... "clockmost RESOLVED: <subject>"
_ZABBIX = re.compile(
    r"^clockmost (RESOLVED|Disaster|High|Average|Warning|Information|Not classified): (.+)$"
)


def by_name(name: str) -> AlertType:
    return _BY_NAME.get(name, UNKNOWN)


def classify(title: str | None) -> AlertType:
    text = title or ""
    for alert_type in CATALOGUE:
        if re.search(alert_type.pattern, text):
            return alert_type
    return UNKNOWN


@dataclass(frozen=True)
class Parsed:
    type: AlertType
    key: str  # what a repeat or a RESOLVED is matched on
    resolves: bool  # a RESOLVED, closing the alert it answers


def parse(title: str | None) -> Parsed:
    """The alert's type, its matching key, and whether it's a RESOLVED.
    Zabbix's severity can change between firings (Warning, then High), so
    the key is the subject without it."""
    text = (title or "").strip()
    match = _ZABBIX.match(text)
    subject = match.group(2) if match else text
    alert_type = classify(subject)
    grouped = re.match(alert_type.group, subject) if alert_type.group else None
    key = grouped.group(0) if grouped else subject
    return Parsed(alert_type, key, bool(match and match.group(1) == "RESOLVED"))
