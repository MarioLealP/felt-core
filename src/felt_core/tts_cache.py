"""A tiny in-memory LRU cache for rendered TTS audio, keyed by the exact
(stripped) text that was spoken plus the emotion it was rendered with
(WP-06, WP-54: the same words in a sleepy vs excited voice are different
audio). In its own module, not api.py or
brain.py, so both can reach it without a circular import between them:
api.py checks it on every real TTS call, and brain.py's weather precompute
loop (which lives there, not here, to avoid the reverse circularity) warms
an entry ahead of time so the very next matching request is a cache hit
with zero felt-voice calls.
"""

import hashlib
from collections import OrderedDict

_MAX_ENTRIES = 64
_cache: "OrderedDict[str, bytes]" = OrderedDict()


def _key(text: str, emotion: str | None) -> str:
    # NUL can't appear in either part, so ("a", "b") never collides with
    # ("a\0b", None)-style joins; None and "" both mean "no emotion".
    return hashlib.sha256(f"{emotion or ''}\0{text}".encode()).hexdigest()


def get(text: str, emotion: str | None = None) -> bytes | None:
    key = _key(text, emotion)
    wav = _cache.get(key)
    if wav is not None:
        _cache.move_to_end(key)
    return wav


def put(text: str, wav_bytes: bytes, emotion: str | None = None) -> None:
    key = _key(text, emotion)
    _cache[key] = wav_bytes
    _cache.move_to_end(key)
    while len(_cache) > _MAX_ENTRIES:
        _cache.popitem(last=False)
