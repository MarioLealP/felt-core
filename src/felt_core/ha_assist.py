"""Home Assistant's own command matcher first (felt-ha-hub, 2026-10-19).

A plain home or list command ("turn off the shelf light", "lights off", "is
the fan on?", "add milk to the shopping list") goes to HA's built-in
conversation agent before her model: HA matches it against the entities
exposed to Assist - their names, aliases and areas, all kept in HA - and
does it. What it can't match goes to the model as before. This replaced
home_fast_path, our own regex for the same job (Mario: "move away from stuff
we have to maintain ourselves where possible").

Felt's HA device sits in Mario's room (area Bedroom), and goes along with
every command: HA reads "lights off" as that room's lights, like a voice
satellite's. Measured before (HA's debug matcher, 35 sentences): 0 of 11
non-commands matched ("turn around", "turn off notifications in KDE", "is the
server on fire?").

Several things onto a list at once ("put eggs and bread on my shopping
list") stay with her model: HA adds the whole phrase as one item (cm-fmy),
her shopping_list skill takes them one by one.
"""

import logging
import re

import httpx

from felt_core.brain.command_words import POLITE, TAIL
from felt_core.config import FeltConfig

logger = logging.getLogger(__name__)

_AGENT = "conversation.home_assistant"
# a command, not a story or a question about something else
_MAX_WORDS = 12
_OPENING = re.compile(rf"^\s*{POLITE}", re.IGNORECASE)
_CLOSING = re.compile(TAIL, re.IGNORECASE)
_HANDLED = ("action_done", "query_answer")
# "eggs and bread ... list", "eggs, bread ... list"
_SEVERAL_ON_A_LIST = re.compile(r"(\band\b|,|&).*\blist\b", re.IGNORECASE)


def clean(message: str) -> str:
    """Pure: the command without the pleasantries HA's sentences don't
    allow - "Felt, can you turn the shelf light on please :)" -> "turn the
    shelf light on"."""
    text = _OPENING.sub("", message, count=1)
    return _CLOSING.sub("", text).strip()


async def handle(config: FeltConfig, message: str) -> str | None:
    """HA's answer ("Turned off the light", "Yes", "5") when it matched and
    did it; None to leave the message to her model."""
    if not (config.ha_url and config.ha_token):
        return None
    text = clean(message)
    if not text or len(text.split()) > _MAX_WORDS or _SEVERAL_ON_A_LIST.search(text):
        return None
    body: dict = {"text": text, "language": "en", "agent_id": _AGENT}
    if config.ha_assist_device:
        body["device_id"] = config.ha_assist_device
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                f"{config.ha_url}/api/conversation/process",
                headers={"Authorization": f"Bearer {config.ha_token}"},
                json=body,
            )
            response.raise_for_status()
            answer = response.json().get("response", {})
    except (httpx.HTTPError, ValueError) as e:
        logger.info("ha assist: Home Assistant didn't answer (%r), her model takes it", e)
        return None
    if answer.get("response_type") not in _HANDLED:
        return None
    speech = answer.get("speech", {}).get("plain", {}).get("speech") or "Done."
    logger.info("ha assist: %r handled by Home Assistant: %s", text, speech)
    return speech
