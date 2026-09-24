"""Per-request latency instrumentation (WP-04). Every speed decision made
about felt-core up to 2026-09-22 (TTS engine choice, caching, model picks,
the Nitro5 Ollama/ComfyUI split) was made from hand-correlating container
log timestamps - this gives every /v1/* request a request_id, times named
stages against it, persists each stage to SQLite for later percentile
queries (`/v1/metrics/latency`), and puts the same numbers directly on the
response via a standard Server-Timing header so a client (felt-tray) can
log its own turn's breakdown without a second round trip.

A metrics failure must never break a real request - every write here is
wrapped in try/except + log, never allowed to propagate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class _RequestMetrics:
    request_id: str
    endpoint: str
    db_path: str
    session_id: str | None
    # (stage, ms, tags) in the order they were recorded - both the
    # Server-Timing header and the SQLite rows are built from this.
    stages: list[tuple[str, float, dict]] = field(default_factory=list)


_current: ContextVar[_RequestMetrics | None] = ContextVar("felt_core_metrics", default=None)


def start_request(request_id: str, endpoint: str, db_path: str, session_id: str | None = None) -> None:
    """Called once by the FastAPI middleware, before the route handler
    runs. Everything `timed()`/`record()` do below reads this same
    ContextVar - contextvars.copy_context() (what asyncio.create_task uses
    under the hood) means a task spawned after this point, like WP-03's
    concurrent privacy-check task, still sees it."""
    _current.set(_RequestMetrics(request_id=request_id, endpoint=endpoint, db_path=db_path, session_id=session_id))


def record(stage: str, ms: float, **tags) -> None:
    metrics = _current.get()
    if metrics is None:
        # Nothing wrapped this call in a request (e.g. a unit test calling
        # brain.chat() directly, or a non-/v1/ path) - not an error, just
        # nothing to record into.
        return
    metrics.stages.append((stage, ms, tags))
    try:
        _save_stage(metrics, stage, ms, tags)
    except Exception:
        logger.exception("failed to persist latency row for stage %r", stage)


@asynccontextmanager
async def timed(stage: str, **tags):
    """Records the stage when it finishes - normally or by raising - but
    NOT when it was cancelled: a cancelled stage (e.g. chat()'s concurrent
    privacy check, cancelled once a skill is picked) never ran to
    completion, so its duration isn't a timing of that stage at all and
    would drag the percentiles down (audit 2026-09-24)."""
    start = time.monotonic()
    try:
        yield
    except asyncio.CancelledError:
        raise
    except BaseException:
        record(stage, (time.monotonic() - start) * 1000.0, **tags)
        raise
    record(stage, (time.monotonic() - start) * 1000.0, **tags)


def _save_stage(metrics: _RequestMetrics, stage: str, ms: float, tags: dict) -> None:
    from felt_core.db import save_latency_row

    save_latency_row(
        metrics.db_path,
        request_id=metrics.request_id,
        endpoint=metrics.endpoint,
        stage=stage,
        ms=ms,
        route=tags.get("route"),
        skill=tags.get("skill"),
        model=tags.get("model"),
        chars=tags.get("chars"),
        session_id=metrics.session_id,
    )


def server_timing_header() -> str | None:
    """Standard Server-Timing syntax:
    `router;dur=3812.4, reply;desc="cloud";dur=612.0, tts;dur=2430.1`.
    None (no header added) when nothing was ever recorded for this
    request, rather than an empty string."""
    metrics = _current.get()
    if metrics is None or not metrics.stages:
        return None
    parts = []
    for stage, ms, tags in metrics.stages:
        desc = tags.get("route") or tags.get("skill") or tags.get("model")
        if desc:
            parts.append(f'{stage};desc="{desc}";dur={ms:.1f}')
        else:
            parts.append(f"{stage};dur={ms:.1f}")
    return ", ".join(parts)


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _stats(values: list[float]) -> dict:
    values = sorted(values)
    return {
        "n": len(values),
        "p50": round(_percentile(values, 0.5), 1),
        "p95": round(_percentile(values, 0.95), 1),
        "max": round(max(values), 1),
    }


def compute_stats(rows: list[dict]) -> dict:
    """Rows: each a dict with at least stage/ms, optionally route/skill.
    Grouped by stage; a stage that carries a route or skill tag also gets
    a breakdown by that tag (e.g. "reply" split into cloud vs local)."""
    by_stage: dict[str, list[float]] = {}
    by_stage_route: dict[str, dict[str, list[float]]] = {}
    by_stage_skill: dict[str, dict[str, list[float]]] = {}

    for row in rows:
        stage = row["stage"]
        ms = row["ms"]
        by_stage.setdefault(stage, []).append(ms)
        if row.get("route"):
            by_stage_route.setdefault(stage, {}).setdefault(row["route"], []).append(ms)
        if row.get("skill"):
            by_stage_skill.setdefault(stage, {}).setdefault(row["skill"], []).append(ms)

    result: dict[str, dict] = {}
    for stage, values in by_stage.items():
        entry = _stats(values)
        if stage in by_stage_route:
            entry["by_route"] = {route: _stats(v) for route, v in by_stage_route[stage].items()}
        if stage in by_stage_skill:
            entry["by_skill"] = {skill: _stats(v) for skill, v in by_stage_skill[stage].items()}
        result[stage] = entry
    return result
