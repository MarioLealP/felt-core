from felt_core.zabbix_client import (
    health_label,
    matching_containers,
    parse_container_items,
    unhealthy_containers,
)

_ITEMS = [
    {
        "key_": 'docker.container_info.state.status["/octoprint"]',
        "lastvalue": "running",
        "lastclock": "100",
    },
    {
        "key_": 'docker.container_info.state.running["/octoprint"]',
        "lastvalue": "1",
        "lastclock": "100",
    },
    {
        "key_": 'docker.container_info.state.health["/octoprint"]',
        "lastvalue": "3",
        "lastclock": "100",
    },
    {
        "key_": 'docker.container_info.state.status["/gitea-server"]',
        "lastvalue": "running",
        "lastclock": "100",
    },
    {
        "key_": 'docker.container_info.state.running["/gitea-server"]',
        "lastvalue": "1",
        "lastclock": "100",
    },
    {
        "key_": 'docker.container_info.state.health["/gitea-server"]',
        "lastvalue": "4",
        "lastclock": "100",
    },
    {
        "key_": 'docker.container_info.state.status["/broken-thing"]',
        "lastvalue": "running",
        "lastclock": "100",
    },
    {
        "key_": 'docker.container_info.state.running["/broken-thing"]',
        "lastvalue": "1",
        "lastclock": "100",
    },
    {
        "key_": 'docker.container_info.state.health["/broken-thing"]',
        "lastvalue": "2",
        "lastclock": "100",
    },
    {
        "key_": 'docker.container_info.state.running["/palworld"]',
        "lastvalue": "0",
        "lastclock": "100",
    },
    {
        "key_": 'docker.container_info.state.health["/palworld"]',
        "lastvalue": "2",
        "lastclock": "100",
    },
    # An unrelated item that happens to also be on this host — must be ignored.
    {"key_": "docker.ping", "lastvalue": "1", "lastclock": "100"},
]

# Real case hit live testing: a one-off Gitea Actions CI job container,
# long gone, whose Zabbix items never received a real value (lastclock
# "0" throughout) — indistinguishable from a currently-existing container
# unless the staleness itself is checked.
_STALE_CI_JOB_ITEMS = [
    {
        "key_": 'docker.container_info.state.status["/GITEA-ACTIONS-TASK-206-WORKFLOW-rust-pkg"]',
        "lastvalue": "",
        "lastclock": "0",
    },
    {
        "key_": 'docker.container_info.state.running["/GITEA-ACTIONS-TASK-206-WORKFLOW-rust-pkg"]',
        "lastvalue": "0",
        "lastclock": "0",
    },
]


def test_parse_container_items_groups_by_name():
    containers = parse_container_items(_ITEMS)
    assert containers["octoprint"] == {"status": "running", "running": "1", "health": "3"}
    assert "docker.ping" not in containers


def test_parse_container_items_excludes_stale_data_less_containers():
    containers = parse_container_items(_ITEMS + _STALE_CI_JOB_ITEMS)
    assert "GITEA-ACTIONS-TASK-206-WORKFLOW-rust-pkg" not in containers
    assert "octoprint" in containers


def test_health_label_mapping():
    assert health_label("3") == "healthy"
    assert health_label("2") == "unhealthy"
    assert health_label("4") == "none"
    assert health_label("99") == "unknown (99)"


def test_matching_containers_exact_match():
    containers = parse_container_items(_ITEMS)
    assert matching_containers(containers, "octoprint") == ["octoprint"]


def test_matching_containers_single_partial_match():
    containers = parse_container_items(_ITEMS)
    assert matching_containers(containers, "octo") == ["octoprint"]


def test_matching_containers_no_match_returns_empty():
    containers = parse_container_items(_ITEMS)
    assert matching_containers(containers, "nonexistent") == []


def test_matching_containers_ambiguous_returns_all_candidates():
    # Real case hit live testing: "gitea" matches both gitea-server and
    # gitea-act-runner — the caller needs to know it's ambiguous, not be
    # told (wrongly) that nothing matched.
    containers = parse_container_items(
        _ITEMS
        + [
            {
                "key_": 'docker.container_info.state.status["/gitea-act-runner"]',
                "lastvalue": "running",
                "lastclock": "100",
            },
            {
                "key_": 'docker.container_info.state.running["/gitea-act-runner"]',
                "lastvalue": "1",
                "lastclock": "100",
            },
            {
                "key_": 'docker.container_info.state.health["/gitea-act-runner"]',
                "lastvalue": "4",
                "lastclock": "100",
            },
        ]
    )
    assert matching_containers(containers, "gitea") == ["gitea-act-runner", "gitea-server"]


def test_unhealthy_containers_flags_by_health_not_running_state():
    containers = parse_container_items(_ITEMS)
    # broken-thing (running) and palworld (stopped) are both flagged because
    # their health code is "2" (unhealthy) — the function keys off health,
    # not running state, since "stopped" alone is often intentional.
    assert unhealthy_containers(containers) == ["broken-thing", "palworld"]


def test_unhealthy_containers_excludes_healthy_and_no_healthcheck():
    containers = parse_container_items(_ITEMS)
    assert "octoprint" not in unhealthy_containers(containers)  # health "3" = healthy
    assert "gitea-server" not in unhealthy_containers(containers)  # health "4" = none
