"""What DJ mode learns, as pure functions: how adventurous to be, and how a
pick went."""

from felt_core.music_taste import is_skip

DEFAULT_DISCOVERY = 1 / 3  # Mario 2026-10-17: "an excuse to find new songs"
_MIN_DISCOVERY, _MAX_DISCOVERY = 0.15, 0.6
_MIN_OUTCOMES = 3


def discovery_share(kept: list[bool]) -> float:
    """How many of her picks should be new to Mario: a third to start, then
    following how her recent discoveries went - skipped most -> fewer
    (never none), kept most -> more."""
    if len(kept) < _MIN_OUTCOMES:
        return DEFAULT_DISCOVERY
    keep_rate = sum(kept) / len(kept)
    return _MIN_DISCOVERY + (_MAX_DISCOVERY - _MIN_DISCOVERY) * keep_rate


def outcome(progress_ms: int, duration_ms: int) -> str:
    """A pick that changed before ~30% played was skipped - the same rule as
    the listening history (music_taste.is_skip)."""
    return "skipped" if is_skip(progress_ms, duration_ms) else "played"
