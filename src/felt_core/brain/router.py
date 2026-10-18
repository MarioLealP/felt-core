"""Semantic router (cm-6b0, Mario 2026-10-18: "we need a more broad context
approach instead of regex"). A message is matched by meaning against the
example phrasings in router_examples.py (bge-small embeddings, CPU, a few
ms), then a context layer weighs in: whether DJ mode is on, whether music
is playing, and - for a short follow-up like "what about the weekend?" -
the previous message. Above `router_bar` it's sure; below, felt-llm with
the whole conversation decides, as today.

For now it runs in SHADOW mode only: chat() asks it alongside the normal
routing and records "router would pick X, Felt did Y" (router_shadow
table) - it never changes what she does, never delays a reply, and a
router that isn't loaded (no model on disk, tests) is just skipped.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from felt_core.brain.router_examples import EXAMPLES
from felt_core.config import FeltConfig

logger = logging.getLogger(__name__)

Embed = Callable[[Sequence[str]], np.ndarray]

# a short message after another one may be a follow-up ("what about the weekend?")
_FOLLOW_UP_WORDS = 5
# the follow-up reading has to beat the message alone by this much
_FOLLOW_UP_MARGIN = 0.02
# routes about music already playing, when it's known nothing is
_MUSIC_ROUTES = ("media_control", "now_playing", "dj_mode/feedback")
_NO_MUSIC_PENALTY = 0.05


@dataclass(frozen=True)
class Context:
    previous: str | None = None
    music_playing: bool | None = None  # None = unknown
    dj_on: bool = False


@dataclass(frozen=True)
class Pick:
    skill: str | None  # None = plain chat, no tool
    route: str | None
    score: float
    sure: bool
    via: str  # "message" | "follow-up"


def skill_of(route: str | None) -> str | None:
    return None if route is None else route.split("/", 1)[0]


class Router:
    def __init__(
        self, embed: Embed, bar: float, examples: dict[str | None, list[str]] | None = None
    ) -> None:
        examples = EXAMPLES if examples is None else examples
        self._embed = embed
        self.bar = bar
        self._routes: list[str | None] = []
        texts: list[str] = []
        for route, phrasings in examples.items():
            for text in phrasings:
                self._routes.append(route)
                texts.append(text)
        self._matrix = _normalized(embed(texts))

    def _scores(self, text: str, ctx: Context) -> dict[str | None, float]:
        sims = self._matrix @ _normalized(self._embed([text]))[0]
        best: dict[str | None, float] = {}
        for route, sim in zip(self._routes, sims, strict=True):
            if route == "dj_mode/feedback" and not ctx.dj_on:
                continue  # "more like this" means nothing without a DJ set
            score = float(sim)
            if ctx.music_playing is False and route in _MUSIC_ROUTES:
                score -= _NO_MUSIC_PENALTY
            best[route] = max(best.get(route, -1.0), score)
        return best

    def pick(self, message: str, ctx: Context | None = None) -> Pick:
        ctx = ctx or Context()
        alone = _top(self._scores(message, ctx))
        route, score, via = alone[0], alone[1], "message"
        short = len(message.split()) <= _FOLLOW_UP_WORDS
        sure_chat = route is None and score >= self.bar
        if ctx.previous and short and not sure_chat:
            joined = _top(self._scores(f"{ctx.previous} {message}", ctx))
            if joined[0] is not None and joined[1] > score + _FOLLOW_UP_MARGIN:
                route, score, via = joined[0], joined[1], "follow-up"
        return Pick(skill_of(route), route, score, score >= self.bar, via)


def _top(scores: dict[str | None, float]) -> tuple[str | None, float]:
    route = max(scores, key=lambda r: scores[r])
    return route, scores[route]


def _normalized(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


# --- the live instance ---------------------------------------------------------

_router: Router | None = None
_pending: set[asyncio.Task] = set()


def fastembed_embedder(config: FeltConfig) -> Embed:
    """bge-small from the image (Dockerfile bakes it in) - never downloads."""
    from fastembed import TextEmbedding

    model = TextEmbedding(
        config.router_model,
        cache_dir=config.router_model_dir,
        threads=2,  # a small share of the box, not every core
        local_files_only=True,
    )
    return lambda texts: np.array(list(model.embed(list(texts))))


async def warm(config: FeltConfig, embedder: Callable[[FeltConfig], Embed] = fastembed_embedder):
    """Load the model and example matrix off the event loop; a missing model
    (local dev, tests) leaves the router off."""
    global _router
    if not config.router_enabled:
        return
    try:
        embed = await asyncio.to_thread(embedder, config)
        _router = await asyncio.to_thread(Router, embed, config.router_bar)
        logger.info(
            "router: loaded (%s, bar %.2f) - shadow mode", config.router_model, config.router_bar
        )
    except Exception as e:  # noqa: BLE001 - optional; Felt routes as before without it
        logger.warning("router: not loaded (%r) - shadow mode off", e)


def reset() -> None:
    global _router
    _router = None


def start_shadow(message: str, ctx: Context) -> asyncio.Task[Pick] | None:
    """Kick off the router's pick in a thread; None when it isn't loaded."""
    if _router is None:
        return None
    router = _router
    task = asyncio.create_task(asyncio.to_thread(router.pick, message, ctx))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


def finish_shadow(
    config: FeltConfig,
    task: asyncio.Task[Pick] | None,
    message: str,
    actual: str | None,
    fast: bool,
) -> None:
    """Record the pick next to what Felt did, whenever the pick is ready -
    never awaited by the reply."""
    if task is None:
        return

    def done(t: asyncio.Task[Pick]) -> None:
        if t.cancelled() or t.exception() is not None:
            logger.warning("router shadow failed: %r", None if t.cancelled() else t.exception())
            return
        pick = t.result()
        agree = pick.skill == actual
        logger.info(
            "router shadow: pick=%s (%.2f%s, %s) actual=%s%s %s",
            pick.skill,
            pick.score,
            "" if pick.sure else " unsure",
            pick.via,
            actual,
            " (fast path)" if fast else "",
            "agree" if agree else "DIFFER",
        )
        try:
            record(config.db_path, message, pick, actual, fast)
        except sqlite3.Error:
            logger.warning("router shadow: couldn't record", exc_info=True)

    task.add_done_callback(done)


async def drain() -> None:
    """Wait for outstanding shadow picks (tests, the e2e harness)."""
    if _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
    await asyncio.sleep(0)


# --- the record ----------------------------------------------------------------

_SCHEMA = """CREATE TABLE IF NOT EXISTS router_shadow (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    message TEXT NOT NULL,
    pick TEXT,
    score REAL NOT NULL,
    sure INTEGER NOT NULL,
    via TEXT NOT NULL,
    actual TEXT,
    fast INTEGER NOT NULL
)"""


def record(db_path: str, message: str, pick: Pick, actual: str | None, fast: bool) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(_SCHEMA)
        conn.execute(
            "INSERT INTO router_shadow (at, message, pick, score, sure, via, actual, fast)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                message[:500],
                pick.skill,
                round(pick.score, 4),
                int(pick.sure),
                pick.via,
                actual,
                int(fast),
            ),
        )


def shadow_rows(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.execute(_SCHEMA)
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM router_shadow ORDER BY id")]
