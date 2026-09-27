"""Felt's outbound push channel — publishes to a dedicated, write-only
ntfy topic. Deliberately a separate credential from the read-only
subscription in ntfy_listener.py: least-privilege, the ability to push
notifications to Mario's phone is not the same credential that can only
read the existing clockmost topic.

Unset config means proactive notifications simply aren't available yet,
not a hardcoded assumption they are.
"""

import httpx

from felt_core.config import FeltConfig


async def notify(
    config: FeltConfig, message: str, title: str | None = None, priority: int = 3
) -> bool:
    if (
        not config.ntfy_url
        or not config.ntfy_publish_topic
        or not config.ntfy_publish_user
        or not config.ntfy_publish_password
    ):
        return False

    headers = {"Priority": str(priority)}
    if title:
        headers["Title"] = title

    async with httpx.AsyncClient(
        auth=(config.ntfy_publish_user, config.ntfy_publish_password), timeout=15
    ) as client:
        response = await client.post(
            f"{config.ntfy_url}/{config.ntfy_publish_topic}", content=message, headers=headers
        )
        return response.status_code == 200
