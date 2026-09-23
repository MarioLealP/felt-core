"""WP-16 Part 3: LRCLIB lyrics. Spotify's public Web API has no lyrics
endpoint, and scraping its private one is a token-hack/ToS path this
project has already ruled out elsewhere (spotify_client.py). LRCLIB
(lrclib.net) is public, keyless, and returns line-synced LRC text when
available - `syncedLyrics` is preferred over `plainLyrics` since only the
synced form carries the per-line timestamps mouth-along/current-line
lookups need.
"""

import json
import logging
import re
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://lrclib.net/api"
# LRCLIB asks integrators to identify themselves; unauthenticated but
# still polite, same posture as poe_trade_cache.py's own User-Agent.
_USER_AGENT = "felt-core/0.1 (personal assistant; contact: lealmariop@gmail.com)"

_LRC_LINE_RE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")
# A chorus needs to actually repeat, not just coincide with a bridge/
# pre-chorus line appearing twice.
_MIN_CHORUS_REPEATS = 3
# Used only for the very last synced line, which has no "next line" to
# bound its mouth-along window against.
_FINAL_LINE_WINDOW_MS = 4000


def parse_lrc(text: str) -> list[dict]:
    """Pure: '[mm:ss.xx]lyric text' lines -> time-sorted
    [{"time_ms": int, "text": str}]. Lines with empty text are kept
    (LRCLIB uses them to mark instrumental gaps - useful timing
    information) but lines with no recognizable timestamp tag at all are
    dropped (metadata tags like [ar:...]/[ti:...]/[length:...])."""
    lines = []
    for raw_line in text.splitlines():
        match = _LRC_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        minutes, seconds, lyric = match.groups()
        time_ms = int((int(minutes) * 60 + float(seconds)) * 1000)
        lines.append({"time_ms": time_ms, "text": lyric.strip()})
    lines.sort(key=lambda line: line["time_ms"])
    return lines


def find_chorus(lines: list[dict]) -> dict | None:
    """Pure: the most-repeated non-empty line (>= _MIN_CHORUS_REPEATS
    occurrences) - returns its first occurrence as
    {"text", "start_ms", "end_ms"}, the window a mouth-along cue timeline
    would be built over. `end_ms` is the next line's start time, or
    `start_ms + _FINAL_LINE_WINDOW_MS` if it's the song's last line.
    None if nothing repeats often enough to call a chorus."""
    counts: dict[str, int] = {}
    for line in lines:
        if line["text"]:
            counts[line["text"]] = counts.get(line["text"], 0) + 1
    if not counts:
        return None

    chorus_text, count = max(counts.items(), key=lambda item: item[1])
    if count < _MIN_CHORUS_REPEATS:
        return None

    for i, line in enumerate(lines):
        if line["text"] == chorus_text:
            end_ms = lines[i + 1]["time_ms"] if i + 1 < len(lines) else line["time_ms"] + _FINAL_LINE_WINDOW_MS
            return {"text": chorus_text, "start_ms": line["time_ms"], "end_ms": end_ms}
    return None  # unreachable - chorus_text always comes from `lines` itself


def current_line(lines: list[dict], position_ms: int) -> str | None:
    """Pure: the lyric line active at `position_ms` - the last line whose
    timestamp has already passed. None before the first line, with no
    lines, or if that line is itself an instrumental gap (empty text)."""
    active = None
    for line in lines:
        if line["time_ms"] <= position_ms:
            active = line
        else:
            break
    return active["text"] if active and active["text"] else None


def _cache_path(cache_dir: Path, track_id: str) -> Path:
    # track_id is a Spotify id (base62, filesystem-safe as-is).
    return cache_dir / f"{track_id}.json"


async def get_lyrics(
    client: httpx.AsyncClient,
    cache_dir: Path,
    track_id: str,
    track_name: str,
    artist_name: str,
    album_name: str,
    duration_s: int,
) -> dict | None:
    """`{"synced": [...], "plain": str | None}`, or None if LRCLIB has
    nothing for this track. Cached permanently on disk, one JSON file per
    track_id (`cache_dir/<track_id>.json`) - lyrics don't change for a
    given track, so this survives a felt-core restart, unlike
    get_currently_playing's short in-process TTL. A genuine "not found"
    IS cached (so a track LRCLIB has nothing for isn't re-requested every
    time it plays); a transient network failure is deliberately NOT
    cached, since that's not the same fact as "no lyrics exist"."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, track_id)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["result"]

    result = None
    try:
        response = await client.get(
            f"{_API_BASE}/get",
            params={
                "track_name": track_name,
                "artist_name": artist_name,
                "album_name": album_name,
                "duration": duration_s,
            },
            headers={"User-Agent": _USER_AGENT},
        )
        if response.status_code == 200:
            body = response.json()
            synced_raw = body.get("syncedLyrics")
            plain = body.get("plainLyrics")
            if synced_raw:
                result = {"synced": parse_lrc(synced_raw), "plain": plain}
            elif plain:
                result = {"synced": [], "plain": plain}
        # Any other status (404 "not found" is LRCLIB's normal response
        # for a genuine miss, per its docs) falls through with result
        # still None, and gets cached as a real miss below.
    except httpx.HTTPError:
        logger.warning("LRCLIB request failed for track_id=%s", track_id, exc_info=True)
        return None

    path.write_text(json.dumps({"result": result}), encoding="utf-8")
    return result
