"""Anything Home Assistant wants Mario told (2026-11-02, Mario: "any text in -> llmed -> notification out").

An automation calls the integration's `felt.tell` action with plain text
("the dehumidifier's tank is full"); she words it like every other line
(her_line), says it at the desk and puts it on the phone. The text itself is
the plain line when felt-llm is down.

No catalogue, no rules: deciding *when* to tell him stays in the HA
automation. `tag` lets a later call replace the earlier phone notification
(the same tag), e.g. "tank emptied" over "tank full".
"""

import logging

from felt_core.clients import phone
from felt_core.config import FeltConfig
from felt_core.core import features, llm_gate
from felt_core.core.her_line import her_line
from felt_core.core.proactive import enqueue_proactive

logger = logging.getLogger(__name__)

FEATURE = "ha.tell"
_TTL_S = 30 * 60


def facts(text: str) -> str:
    """Pure: what her model is told."""
    return f"Home Assistant (the house) wants Mario to know: {text}"


async def tell(config: FeltConfig, text: str, title: str, tag: str) -> str | None:
    """Her line for `text`, said at the desk and on the phone.

    None when the feature is off.
    """
    if not features.enabled(config.db_path, FEATURE):
        return None
    with llm_gate.timely("ha-tell"):
        line = await her_line(config, facts(text), text)
    enqueue_proactive(config, "ha", line, ttl_s=_TTL_S)
    try:
        await phone.notify(config, title, line, tag=f"felt-ha-{tag}")
    except phone.PhoneUnavailableError:
        logger.info("ha_tell: phone unreachable, desk only")
    logger.info("ha_tell: %r -> %r", text, line)
    return line
