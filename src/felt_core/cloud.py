"""The single choke point for every cloud (Gemini API) call felt-core
makes (WP-05). Before this, `brain.py`'s `_cloud_chat` talked straight to
the OpenAI-compatible endpoint with one hardcoded model and no quota
awareness - fine when there was one call site, not once WP-06/WP-07 add
many more. This purpose-routes each call across three free-tier models
(a high-volume Gemma model for vision/routing/phrasing, a conversation
model, and an overflow model) and governs it against Google AI Studio's
real per-model RPM/TPM/RPD limits (checked 2026-09-22 against the actual
configured key - see this WP's implementation notes for the full table
and model ids) so no code path can blow through the daily quota and leave
every later caller broken for the rest of the day.

Uses the native REST `models/{id}:generateContent` endpoint, not the
OpenAI-compatible one - needed for image input (`inline_data`, base64
JPEG), which the OpenAI-compatible shim doesn't support the same way.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import httpx

from felt_core import metrics
from felt_core.config import FeltConfig

logger = logging.getLogger(__name__)

Purpose = Literal["conversation", "screen_reaction", "routing", "skill_phrasing", "summary", "event_reaction"]

_PACIFIC = ZoneInfo("America/Los_Angeles")
_REST_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Google AI Studio dashboard, Mario's project, free tier - confirmed
# 2026-09-22. Per model, per project; daily reset at midnight Pacific;
# there's no remaining-quota response header, so this table (optionally
# overridden per-model by FELT_CLOUD_LIMITS) is the only source of truth
# for what "exhausted" means.
_DEFAULT_LIMITS: dict[str, dict[str, int]] = {
    "gemma-4-26b-a4b-it": {"rpm": 30, "tpm": 16_000, "rpd": 14_400},
    "gemma-4-31b-it": {"rpm": 30, "tpm": 16_000, "rpd": 14_400},
    "gemini-3.5-flash-lite": {"rpm": 15, "tpm": 250_000, "rpd": 500},
    "gemini-3.1-flash-lite": {"rpm": 15, "tpm": 250_000, "rpd": 500},
}

# screen_reaction shares the volume model with routing/skill_phrasing -
# capped to 60% of ITS OWN model's RPD so a burst of screen reactions
# can't starve the other two purposes for the rest of the day (Mario's
# rule is "always spoken," but that can't come at routing/phrasing's
# expense). Applied on EVERY model screen_reaction can land on, overflow
# included - otherwise, once the volume model's share is used up, screen
# traffic drains the overflow model (conversation's only fallback) with
# no cap at all (audit 2026-09-24).
_SCREEN_REACTION_RPD_SHARE = 0.6
# Stay this far under the real limit before it's actually hit - a
# request mid-flight when a counter ticks over shouldn't be the one that
# draws the 429.
_SAFETY_MARGIN = 0.95

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class CloudUnavailable(Exception):
    """No model - volume, conversation, nor overflow, whichever pair
    applies to this purpose - can take the call right now (all exhausted
    or over budget, or nothing configured). Callers catch this and fall
    back to the existing local path, unchanged."""


class _CloudHttpError(Exception):
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"cloud API returned {status_code}: {body}")


# Remembered per process lifetime, not persisted - a model's actual
# systemInstruction support doesn't change without a felt-core restart
# anyway (a new deploy would pick a possibly-different model id).
_no_system_instruction: set[str] = set()
# Same idea for thinkingConfig.thinkingLevel (see _THINKING_LEVELS).
_no_thinking_config: set[str] = set()

# Short spoken lines don't need the model to reason first. Found live
# 2026-09-24 (WP-06 audit, 71/84 weather precomputes fell back to raw
# tool output): gemma-4-26b-a4b-it spent all 997 of its 1000
# maxOutputTokens on hidden thinking (finishReason MAX_TOKENS, one
# `thought: true` part, no answer part). With thinkingLevel "minimal" the
# same prompt answers in ~22 tokens with finishReason STOP. Verified
# accepted by gemma-4-26b-a4b-it, gemini-3.1-flash-lite and
# gemini-3.5-flash-lite; `thinkingBudget: 0` is rejected by Gemma.
_THINKING_LEVELS: dict[str, str] = {
    "skill_phrasing": "minimal",
    "event_reaction": "minimal",
}

# In-memory only, by design: RPM/TPM windows are 60s wide, so losing them
# on a restart costs at most one minute of undercounting, not worth a
# SQLite round trip per call the way the RPD counter (cloud_usage table,
# db.py) needs to be to survive a mid-day restart.
_recent_calls: dict[str, list[float]] = {}
_recent_tokens: dict[str, list[tuple[float, int]]] = {}
# model -> (exhausted_until_epoch_seconds, "rpd" | "rpm")
_exhausted_until: dict[str, tuple[float, str]] = {}


def _purpose_models(purpose: Purpose, config: FeltConfig) -> list[str]:
    if purpose in ("screen_reaction", "routing", "skill_phrasing", "event_reaction"):
        return [m for m in (config.cloud_model_volume, config.cloud_model_overflow) if m]
    return [m for m in (config.cloud_model_conversation, config.cloud_model_overflow) if m]


def _limits(config: FeltConfig) -> dict[str, dict[str, int]]:
    limits = {model: dict(vals) for model, vals in _DEFAULT_LIMITS.items()}
    if config.cloud_limits:
        try:
            override = json.loads(config.cloud_limits)
        except json.JSONDecodeError:
            logger.warning("FELT_CLOUD_LIMITS isn't valid JSON - ignoring override: %r", config.cloud_limits)
            override = {}
        for model, vals in override.items():
            limits.setdefault(model, {}).update(vals)
    return limits


def _pacific_day(now: datetime | None = None) -> str:
    return (now or datetime.now(_PACIFIC)).astimezone(_PACIFIC).date().isoformat()


def _next_pacific_midnight_epoch(now: datetime | None = None) -> float:
    current = now or datetime.now(_PACIFIC)
    current = current.astimezone(_PACIFIC)
    tomorrow = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()


def _estimate_tokens(system: str, messages: list[dict], images: list[bytes] | None) -> int:
    chars = len(system) + sum(len(m.get("content", "")) for m in messages)
    tokens = chars // 4
    if images:
        tokens += 1000 * len(images)
    return tokens


def _is_exhausted(model: str, now: float) -> bool:
    entry = _exhausted_until.get(model)
    if entry is None:
        return False
    until, _reason = entry
    if now >= until:
        del _exhausted_until[model]
        return False
    return True


def _mark_exhausted(model: str, body: dict, now: float | None = None) -> None:
    message = str(body.get("error", {}).get("message", "")).lower()
    now = time.time() if now is None else now
    if "day" in message or "daily" in message:
        until = _next_pacific_midnight_epoch(datetime.fromtimestamp(now, tz=_PACIFIC))
        reason = "rpd"
    else:
        # "decide from the error body if possible, else assume RPM" (WP-05)
        until = now + 60.0
        reason = "rpm"
    _exhausted_until[model] = (until, reason)


def _has_rpd_budget(config: FeltConfig, model: str, purpose: Purpose, pt_day: str, limits: dict) -> bool:
    from felt_core.db import get_model_usage_today, get_purpose_usage_today

    rpd = limits.get(model, {}).get("rpd")
    if rpd is not None:
        used = get_model_usage_today(config.db_path, model, pt_day)
        if used >= rpd * _SAFETY_MARGIN:
            return False
        if purpose == "screen_reaction":
            screen_used = get_purpose_usage_today(config.db_path, model, "screen_reaction", pt_day)
            if screen_used >= rpd * _SCREEN_REACTION_RPD_SHARE:
                return False
    return True


def _has_rpm_budget(model: str, now: float, limits: dict) -> bool:
    rpm = limits.get(model, {}).get("rpm")
    if rpm is None:
        return True
    calls = _recent_calls.setdefault(model, [])
    while calls and now - calls[0] > 60.0:
        calls.pop(0)
    return len(calls) < rpm * _SAFETY_MARGIN


def _has_tpm_budget(model: str, now: float, est_tokens: int, limits: dict) -> bool:
    tpm = limits.get(model, {}).get("tpm")
    if tpm is None:
        return True
    entries = _recent_tokens.setdefault(model, [])
    while entries and now - entries[0][0] > 60.0:
        entries.pop(0)
    used = sum(t for _, t in entries)
    return used + est_tokens < tpm * _SAFETY_MARGIN


def _record_call(config: FeltConfig, model: str, purpose: Purpose, pt_day: str, now: float, est_tokens: int) -> None:
    _recent_calls.setdefault(model, []).append(now)
    _recent_tokens.setdefault(model, []).append((now, est_tokens))
    try:
        from felt_core.db import increment_cloud_usage

        increment_cloud_usage(config.db_path, model, purpose, pt_day)
    except Exception:
        logger.exception("failed to persist cloud usage row for %s/%s", model, purpose)


def _build_contents(messages: list[dict], images: list[bytes] | None) -> list[dict]:
    contents = [
        {"role": "model" if m.get("role") == "assistant" else "user", "parts": [{"text": m.get("content", "")}]}
        for m in messages
    ]
    if images:
        image_parts = [
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode("ascii")}}
            for img in images
        ]
        if contents and contents[-1]["role"] == "user":
            contents[-1] = {"role": "user", "parts": contents[-1]["parts"] + image_parts}
        else:
            contents.append({"role": "user", "parts": image_parts})
    return contents


def _build_payload(system: str, contents: list[dict], generation_config: dict, use_system_field: bool) -> dict:
    contents = [dict(c, parts=list(c["parts"])) for c in contents]  # don't mutate the shared list across retries
    payload: dict = {}
    if use_system_field:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    else:
        if contents and contents[0]["role"] == "user":
            contents[0] = {"role": "user", "parts": [{"text": system}] + contents[0]["parts"]}
        else:
            contents = [{"role": "user", "parts": [{"text": system}]}] + contents
    payload["contents"] = contents or [{"role": "user", "parts": [{"text": system}]}]
    if generation_config:
        payload["generationConfig"] = generation_config
    return payload


def _extract_text(body: dict) -> str:
    """The Gemma 4 models emit hidden 'thinking' parts ahead of the real
    answer (found live, 2026-09-22, WP-06: the weather precompute's first
    real run came back as a raw chain-of-thought transcript, not a spoken
    line) - each such part is flagged `"thought": true` in the response
    and isn't meant to be surfaced. Skip those, return the first part
    that actually has real text. If every part is a thought (truncated
    mid-reasoning - see _SKILL_PHRASING_MAX_TOKENS's comment for why that
    should now be rare, not impossible), there's honestly nothing to say
    - empty string, same as any other "nothing usable came back" case
    already handled by every caller."""
    try:
        parts = body["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    for part in parts:
        if part.get("thought"):
            continue
        text = part.get("text")
        if text:
            return text
    return ""


def _extract_json_block(text: str) -> str:
    match = _JSON_BLOCK_RE.search(text)
    return match.group(0) if match else text


def _looks_like_system_instruction_rejection(body: dict) -> bool:
    message = str(body.get("error", {}).get("message", "")).lower()
    return "developer instruction" in message or "systeminstruction" in message


async def _post_generate_content(model: str, payload: dict, config: FeltConfig) -> tuple[int, dict]:
    """The one real network call - kept tiny and easily monkeypatched in
    tests (returns (status_code, json_body) rather than a full
    httpx.Response, so a fake doesn't need to construct one)."""
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{_REST_BASE}/models/{model}:generateContent",
            params={"key": config.gemini_api_key},
            json=payload,
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body


async def _generate(
    model: str,
    system: str,
    contents: list[dict],
    json_mode: bool,
    max_tokens: int | None,
    config: FeltConfig,
    thinking_level: str | None = None,
) -> str:
    generation_config: dict = {}
    if max_tokens is not None:
        generation_config["maxOutputTokens"] = max_tokens
    if json_mode:
        generation_config["responseMimeType"] = "application/json"
    if thinking_level and model not in _no_thinking_config:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}

    use_system_field = model not in _no_system_instruction
    payload = _build_payload(system, contents, generation_config, use_system_field)
    status, body = await _post_generate_content(model, payload, config)

    if status == 400 and "thinkingConfig" in generation_config and "thinking" in str(body).lower():
        logger.info("model %r rejected thinkingConfig - retrying without it", model)
        _no_thinking_config.add(model)
        generation_config = {k: v for k, v in generation_config.items() if k != "thinkingConfig"}
        payload = _build_payload(system, contents, generation_config, use_system_field)
        status, body = await _post_generate_content(model, payload, config)

    # Gemma models on the Gemini API have historically rejected
    # systemInstruction ("developer instruction not enabled") - retry
    # once with the system text folded into the first user turn instead,
    # and remember it so every later call to this model skips straight
    # to that form.
    if status == 400 and use_system_field and _looks_like_system_instruction_rejection(body):
        logger.info("model %r rejected systemInstruction - retrying with it in the first user turn", model)
        _no_system_instruction.add(model)
        payload = _build_payload(system, contents, generation_config, False)
        status, body = await _post_generate_content(model, payload, config)

    if status == 400 and json_mode and "responseMimeType" in generation_config:
        logger.info("model %r rejected responseMimeType - retrying as a plain-text JSON instruction", model)
        generation_config = {k: v for k, v in generation_config.items() if k != "responseMimeType"}
        retry_system = system + "\n\nRespond with only a JSON object, no other text."
        payload = _build_payload(retry_system, contents, generation_config, model not in _no_system_instruction)
        status, body = await _post_generate_content(model, payload, config)
        if status == 200:
            return _extract_json_block(_extract_text(body))

    if status != 200:
        raise _CloudHttpError(status, body)

    return _extract_text(body)


async def cloud_generate(
    purpose: Purpose,
    system: str,
    messages: list[dict],
    config: FeltConfig,
    images: list[bytes] | None = None,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """Purpose-routes to the right free-tier model, checks the governor
    (RPD/RPM/TPM budgets + any live 429 exhaustion), falls back within
    the purpose's model pair, and raises CloudUnavailable if nothing can
    take the call - callers fall back to the existing local path exactly
    as before this WP.

    `config` isn't in the WP's own sketch of this signature, but every
    other module-level function in this codebase takes it explicitly
    (privacy.py, router.py, brain.py) rather than reaching for a global -
    kept that convention here too.
    """
    if not config.gemini_api_key:
        raise CloudUnavailable("no Gemini API key configured")

    # The Gemini API only has user/model turns - a "system"-role entry in
    # `messages` would be sent as if Mario had said it. Any such entry (a
    # context note a caller mixed into its history) is folded into the
    # real system instruction instead.
    system_notes = [m.get("content", "") for m in messages if m.get("role") == "system"]
    if system_notes:
        system = "\n\n".join([system, *system_notes])
        messages = [m for m in messages if m.get("role") != "system"]

    candidates = _purpose_models(purpose, config)
    if not candidates:
        raise CloudUnavailable(f"no cloud model configured for purpose {purpose!r}")

    limits = _limits(config)
    now = time.time()
    pt_day = _pacific_day()
    contents = _build_contents(messages, images)
    est_tokens = _estimate_tokens(system, messages, images)

    last_error: Exception | None = None
    for model in candidates:
        if _is_exhausted(model, now):
            logger.info("cloud model %r is marked exhausted, skipping for purpose %r", model, purpose)
            continue
        if not _has_rpd_budget(config, model, purpose, pt_day, limits):
            logger.info("cloud model %r is at its RPD budget, skipping for purpose %r", model, purpose)
            continue
        if not _has_rpm_budget(model, now, limits):
            logger.info("cloud model %r is at its RPM budget, skipping for purpose %r", model, purpose)
            continue
        if not _has_tpm_budget(model, now, est_tokens, limits):
            logger.info("cloud model %r is at its TPM budget, skipping for purpose %r", model, purpose)
            continue

        try:
            async with metrics.timed("cloud", model=model, purpose=purpose):
                text = await _generate(
                    model, system, contents, json_mode, max_tokens, config, _THINKING_LEVELS.get(purpose)
                )
            _record_call(config, model, purpose, pt_day, now, est_tokens)
            return text
        except _CloudHttpError as e:
            last_error = e
            if e.status_code == 429:
                _mark_exhausted(model, e.body, now)
                logger.warning(
                    "cloud model %r hit 429 for purpose %r - marked exhausted, trying next candidate",
                    model,
                    purpose,
                )
                continue
            # Any other API error (400/500/503...): try the purpose's next
            # model, and if none is left surface it as CloudUnavailable like
            # every other cloud failure - so every caller's fallback (the
            # local path, felt-tray's felt-vision path on a 503) actually
            # runs instead of the request 500ing (audit 2026-09-24).
            logger.warning("cloud model %r failed for purpose %r (%s) - trying next candidate", model, purpose, e)
            continue
        except httpx.HTTPError as e:
            last_error = e
            logger.warning("cloud model %r unreachable for purpose %r (%r) - trying next candidate", model, purpose, e)
            continue

    raise CloudUnavailable(f"no available cloud model for purpose {purpose!r}") from last_error


def cloud_status(config: FeltConfig) -> dict:
    """Backs GET /v1/metrics/cloud: today's counts per model/purpose, the
    effective limits (defaults + any FELT_CLOUD_LIMITS override), any
    live exhausted-until state, and the next Pacific-midnight reset."""
    from felt_core.db import get_cloud_usage_today

    now = time.time()
    pt_day = _pacific_day()
    exhausted = {model: {"until_epoch": until, "reason": reason} for model, (until, reason) in _exhausted_until.items() if now < until}
    return {
        "pt_day": pt_day,
        "next_reset_epoch": _next_pacific_midnight_epoch(),
        "models": {
            "conversation": config.cloud_model_conversation,
            "overflow": config.cloud_model_overflow,
            "volume": config.cloud_model_volume,
        },
        "limits": _limits(config),
        "usage": get_cloud_usage_today(config.db_path, pt_day),
        "exhausted": exhausted,
    }
