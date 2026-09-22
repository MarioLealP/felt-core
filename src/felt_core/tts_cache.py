"""A tiny in-memory LRU cache for rendered TTS audio, keyed by the exact
(stripped) text that was spoken (WP-06). In its own module, not api.py or
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


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get(text: str) -> bytes | None:
    key = _key(text)
    wav = _cache.get(key)
    if wav is not None:
        _cache.move_to_end(key)
    return wav


def put(text: str, wav_bytes: bytes) -> None:
    key = _key(text)
    _cache[key] = wav_bytes
    _cache.move_to_end(key)
    while len(_cache) > _MAX_ENTRIES:
        _cache.popitem(last=False)
