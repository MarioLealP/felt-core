"""When a desktop event gets a line at all: per-event cooldowns, dedupe and minimum durations.

FELT_EVENT_POLICIES overrides the defaults per deployment.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ConfigDict, ValidationError

from felt_core.config import FeltConfig
from felt_core.core.event_prompt import _game_name, _number, _text

logger = logging.getLogger(__name__)


class EventPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Minimum seconds between two spoken lines for this event type.
    cooldown_s: int
    # Minimum seconds before the SAME thing (same track, same game) gets a
    # line again, even if the event cooldown has passed. 0 = no dedupe.
    dedupe_s: int = 0
    # For events that carry details.duration_s: below this, no line.
    min_duration_s: int = 0
    # X-Emotion sent with the line (the WP-07 fixed set).
    emotion: str = "neutral"
    # Mario-triggered events (unhushed) speak even if she just said
    # something: the global min gap is for her unprompted chatter.
    bypass_min_gap: bool = False


# WP-17's table: game_stop -> "finally done?", afk_end -> a greeting,
# spoken only after a long absence; game_start (comfy pose), gpu_hot (a
# cool-off drink from her water bottle) and call_join_guess (a small wave) are mainly non-verbal on the
# desktop side, so their lines are kept rare (long cooldowns). Override
# any field per deployment with FELT_EVENT_POLICIES (JSON).
DEFAULT_POLICIES: dict[str, EventPolicy] = {
    "music_new_track": EventPolicy(cooldown_s=5 * 60, dedupe_s=60 * 60, emotion="happy"),
    # WP-48 DJ mode: a line as each of her picks comes on. He asked for the
    # set (and said she can yap), so no cooldown and no min gap.
    "dj_track": EventPolicy(cooldown_s=0, emotion="happy", bypass_min_gap=True),
    "game_start": EventPolicy(cooldown_s=20 * 60, dedupe_s=2 * 60 * 60, emotion="excited"),
    "game_stop": EventPolicy(cooldown_s=20 * 60, min_duration_s=20 * 60, emotion="amused"),
    "afk_end": EventPolicy(cooldown_s=30 * 60, min_duration_s=30 * 60, emotion="happy"),
    "gpu_hot": EventPolicy(cooldown_s=60 * 60, emotion="surprised"),
    "call_join_guess": EventPolicy(cooldown_s=20 * 60, emotion="happy"),
    # WP-29 v2: Mario pressed Exiled Exchange 2's price-check key on an item.
    # He asked, so no min gap; a few seconds against a double press only.
    "poe_item": EventPolicy(cooldown_s=4, emotion="excited", bypass_min_gap=True),
    # Mario lifting a hush (felt-animation /unhush?announce=1, 2026-09-26:
    # "give her some snappy comments about being unmuted"). He triggered
    # it, so only a small anti-double-press cooldown and no min gap.
    "unhushed": EventPolicy(cooldown_s=20, emotion="amused", bypass_min_gap=True),
}


def policies(config: FeltConfig) -> dict[str, EventPolicy]:
    table = dict(DEFAULT_POLICIES)
    if config.event_policies:
        try:
            override = json.loads(config.event_policies)
        except json.JSONDecodeError:
            logger.warning("FELT_EVENT_POLICIES isn't valid JSON - ignoring it")
            override = {}
        for event, fields in override.items():
            if event in table and isinstance(fields, dict):
                known = {k: v for k, v in fields.items() if k in EventPolicy.model_fields}
                try:
                    table[event] = EventPolicy.model_validate({**table[event].model_dump(), **known})
                except ValidationError as error:
                    logger.warning("FELT_EVENT_POLICIES[%s] ignored: %s", event, error)
    return table


def dedupe_key(event: str, details: dict) -> str | None:
    """Pure: what counts as "the same thing" for an event's dedupe window."""
    if event == "music_new_track":
        key = f"{_text(details, 'title').lower()}|{_text(details, 'artist').lower()}"
        return key if key != "|" else None
    if event in ("game_start", "game_stop"):
        return _game_name(details).lower() or None
    return None


def decide(
    event: str,
    details: dict,
    table: dict[str, EventPolicy],
    *,
    min_gap_s: int,
    since_any_remark: float | None,
    since_event: float | None,
    since_same: float | None,
) -> str | None:
    """Pure: None = speak, otherwise the reason not to. The `since_*` ages
    are seconds since the last spoken remark of any kind / of this event /
    about this same thing (None = never).
    """
    policy = table.get(event)
    if policy is None:
        return "unknown_event"
    if policy.min_duration_s:
        duration = _number(details, "duration_s")
        if duration is None or duration < policy.min_duration_s:
            return "too_short"
    if not policy.bypass_min_gap and since_any_remark is not None and since_any_remark < min_gap_s:
        return "min_gap"
    if since_event is not None and since_event < policy.cooldown_s:
        return "cooldown"
    if policy.dedupe_s and since_same is not None and since_same < policy.dedupe_s:
        return "duplicate"
    return None
