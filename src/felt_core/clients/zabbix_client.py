"""Parsing helpers for get_container_status — reuses clockmost's existing
Zabbix monitoring (Docker-by-Zabbix-agent-2 template) rather than adding a
separate container health-check system. Split out from the network calls
so it's unit testable without a live Zabbix instance.
"""

_HEALTH_LABELS = {
    "0": "unknown",
    "1": "starting",
    "2": "unhealthy",
    "3": "healthy",
    "4": "none",
}


def health_label(code: str) -> str:
    return _HEALTH_LABELS.get(code, f"unknown ({code})")


def parse_container_items(items: list[dict]) -> dict[str, dict]:
    """Groups Zabbix `docker.container_info.state.*[...]` items (each a
    dict with `key_`, `lastvalue`, and `lastclock`, as returned by
    `item.get`) by container name — the bracketed part of the key,
    leading '/' stripped.

    Excludes containers where every item has `lastclock == "0"` — Zabbix's
    sentinel for "never received a real value." Real 2026-09-12 finding:
    one-off Gitea Actions CI job containers leave behind exactly these
    stale, data-less items indefinitely (their containers are long gone),
    and their long generated names swamped ordinary lookups like "gitea"
    with irrelevant noise. Filtering on "has this ever reported real
    data" is a generic staleness check, not a hardcoded name pattern, so
    it isn't tied to Gitea Actions specifically.
    """
    containers: dict[str, dict] = {}
    has_data: dict[str, bool] = {}
    for item in items:
        key = item["key_"]
        if not key.startswith("docker.container_info.state.") or "[" not in key:
            continue
        field = key.split(".state.", 1)[1].split("[", 1)[0]
        name = key.split("[", 1)[1].rstrip("]").strip('"').lstrip("/")
        containers.setdefault(name, {})[field] = item["lastvalue"]
        has_data[name] = has_data.get(name, False) or item.get("lastclock", "0") != "0"
    return {name: fields for name, fields in containers.items() if has_data.get(name)}


def matching_containers(containers: dict[str, dict], query: str) -> list[str]:
    """Case-insensitive match of `query` against known container names —
    an exact match wins outright (a single-element list); otherwise every
    substring match is returned, so the caller can tell "no match" (empty
    list) apart from "ambiguous, pick one" (more than one) instead of
    collapsing both into the same "not found" response.
    """
    query = query.lower()
    exact = [name for name in containers if name.lower() == query]
    if exact:
        return exact
    return sorted(name for name in containers if query in name.lower())


def unhealthy_containers(containers: dict[str, dict]) -> list[str]:
    """Container names Docker's own health check currently reports as
    'unhealthy' (code '2') — an unambiguous problem signal, unlike merely
    'not running', which is often just an intentionally-stopped service
    and can't be judged as good or bad without knowing what's expected.
    """
    return sorted(name for name, info in containers.items() if info.get("health") == "2")
