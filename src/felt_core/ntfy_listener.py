"""Consumes Mario's existing ntfy topic (assistant-automation.md) —
not a new notification system, just Felt listening to the same channel
he already gets alerts on.

Uses ntfy's long-lived JSON stream endpoint (one JSON object per line,
held open indefinitely) rather than polling. Runs as a background task
for the lifetime of the service; reconnects on any error rather than
dying, since a dropped connection shouldn't take the listener down
permanently.

WP-42.5: it remembers the last message it saw and reconnects with
`since=<that id>` (ntfy keeps 48 h), so alerts sent while felt-core was down
or restarting still become incidents, at their own time - found live when a
test alert during a felt-core stop never reached Systems.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime

import httpx

from felt_core import incidents, responder
from felt_core.config import FeltConfig
from felt_core.db import get_setting, save_ntfy_event, set_setting

LAST_ID = "ntfy_last_id"


def stream_params(db_path: str) -> dict:
    """Catch up from the last message seen; live only on the very first run."""
    last = get_setting(db_path, LAST_ID)
    return {"since": last} if last else {}


def sent_at(event: dict) -> datetime | None:
    """When ntfy got it (a caught-up alert keeps its own time)."""
    try:
        return datetime.fromtimestamp(int(event["time"]), UTC)
    except (KeyError, TypeError, ValueError):
        return None


logger = logging.getLogger(__name__)


def _basic_auth(user: str | None, password: str | None) -> tuple[str, str] | None:
    """`httpx.AsyncClient(auth=...)` wants either a real `(user, password)`
    pair or `None` - never a pair with a `None` half. A user set without a
    password (WP-40 audit: `config.ntfy_password` is independently
    Optional) used to build exactly that broken half-pair and would have
    failed at the first real connection; treat it the same as "no auth
    configured" instead."""
    if user and password:
        return (user, password)
    return None


async def listen(config: FeltConfig) -> None:
    if not config.ntfy_url or not config.ntfy_topic:
        logger.info("ntfy not configured, listener not starting")
        return

    url = f"{config.ntfy_url}/{config.ntfy_topic}/json"
    auth = _basic_auth(config.ntfy_user, config.ntfy_password)

    while True:
        try:
            async with httpx.AsyncClient(auth=auth, timeout=None) as client:
                params = stream_params(config.db_path)
                if params:
                    logger.info("ntfy: catching up since %s", params["since"])
                async with client.stream("GET", url, params=params) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if event.get("event") != "message":
                            continue
                        event_id = save_ntfy_event(
                            config.db_path,
                            event.get("title"),
                            event.get("message", ""),
                            event.get("priority"),
                        )
                        # WP-44: she's the first responder - every alert
                        # becomes (or joins) an incident
                        try:
                            incidents.intake(
                                config.db_path,
                                event.get("title"),
                                event.get("message", ""),
                                at=sent_at(event),
                                ntfy_event_id=event_id,
                            )
                            # WP-58: if it's Claude's, wake the responder now
                            responder.wake(config.db_path)
                        except Exception:
                            logger.exception("incident intake failed for %r", event.get("title"))
                        if event.get("id"):
                            set_setting(config.db_path, LAST_ID, str(event["id"]))
                        logger.info("ntfy event saved: %s", event.get("message"))
        except Exception:
            logger.exception("ntfy listener error, reconnecting in 10s")
            await asyncio.sleep(10)
