"""Consumes Mario's existing ntfy topic (assistant-automation.md) —
not a new notification system, just Felt listening to the same channel
he already gets alerts on.

Uses ntfy's long-lived JSON stream endpoint (one JSON object per line,
held open indefinitely) rather than polling. Runs as a background task
for the lifetime of the service; reconnects on any error rather than
dying, since a dropped connection shouldn't take the listener down
permanently.
"""

import asyncio
import json
import logging

import httpx

from felt_core.config import FeltConfig
from felt_core.db import save_ntfy_event

logger = logging.getLogger(__name__)


async def listen(config: FeltConfig) -> None:
    if not config.ntfy_url or not config.ntfy_topic:
        logger.info("ntfy not configured, listener not starting")
        return

    url = f"{config.ntfy_url}/{config.ntfy_topic}/json"
    auth = (config.ntfy_user, config.ntfy_password) if config.ntfy_user else None

    while True:
        try:
            async with httpx.AsyncClient(auth=auth, timeout=None) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if event.get("event") != "message":
                            continue
                        save_ntfy_event(
                            config.db_path,
                            event.get("title"),
                            event.get("message", ""),
                            event.get("priority"),
                        )
                        logger.info("ntfy event saved: %s", event.get("message"))
        except Exception:
            logger.exception("ntfy listener error, reconnecting in 10s")
            await asyncio.sleep(10)
