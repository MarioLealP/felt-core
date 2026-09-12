"""Docker container health on the clockmost server, via Zabbix's own
existing monitoring (Docker-by-Zabbix-agent-2 template already tracks
every container) rather than building a separate health-check path.
"""

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult
from felt_core.zabbix_client import health_label, matching_containers, parse_container_items, unhealthy_containers

name = "get_container_status"
description = (
    "Checks whether a specific service/container on the clockmost server is "
    "running and healthy, or (with no name given) reports any containers "
    "currently failing their health check."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "container": {
            "type": "string",
            "description": "name of the service/container to check (optional — omit for an overall health summary)",
        }
    },
}


async def _zabbix_call(client: httpx.AsyncClient, config, method: str, params: dict):
    response = await client.post(
        f"{config.zabbix_url}/api_jsonrpc.php",
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        headers={
            "Content-Type": "application/json-rpc",
            "Authorization": f"Bearer {config.zabbix_api_token}",
        },
    )
    response.raise_for_status()
    return response.json()["result"]


async def handle(params, ctx):
    config = ctx.config
    if not config.zabbix_url or not config.zabbix_api_token:
        return SkillResult(text="Container status check isn't configured (no Zabbix URL/token set).")

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            hosts = await _zabbix_call(
                client, config, "host.get", {"output": ["hostid"], "filter": {"host": [config.zabbix_host]}}
            )
            if not hosts:
                return SkillResult(text=f"No Zabbix host named '{config.zabbix_host}' found.")
            items = await _zabbix_call(
                client,
                config,
                "item.get",
                {"output": ["key_", "lastvalue", "lastclock"], "hostids": hosts[0]["hostid"]},
            )
        except httpx.HTTPError:
            return SkillResult(text="Couldn't reach Zabbix right now.")

    containers = parse_container_items(items)

    query = get_single_param(params, "container", "name", "service")
    if query:
        matches = matching_containers(containers, str(query))
        if not matches:
            return SkillResult(text=f"I don't see a container matching '{query}'.")
        if len(matches) > 1:
            return SkillResult(text=f"Multiple containers match '{query}': {', '.join(matches)}. Which one?")
        matched = matches[0]
        info = containers[matched]
        running = info.get("running") == "1"
        return SkillResult(
            text=(
                f"{matched}: {'running' if running else 'not running'}, "
                f"health check: {health_label(info.get('health', '0'))}"
            )
        )

    unhealthy = unhealthy_containers(containers)
    if not unhealthy:
        return SkillResult(text="No containers are currently failing their health check.")
    return SkillResult(text="Unhealthy containers: " + ", ".join(unhealthy))
