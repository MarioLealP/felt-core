"""WP-16 Part 3: LRCLIB lyrics. Spotify's public Web API has no lyrics
endpoint, and scraping its private one is a token-hack/ToS path this
project has already ruled out elsewhere (spotify_client.py). LRCLIB
(lrclib.net) is public, keyless, and returns line-synced LRC text when
available - `syncedLyrics` is preferred over `plainLyrics` since only the
synced form carries the per-line timestamps mouth-along/current-line
lookups need.
"""

import hashlib
import json
import logging
import re
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://lrclib.net/api"
# LRCLIB asks integrators to identify themselves; unauthenticated but
# still polite. The real value comes from config (FELT_LYRICS_USER_AGENT)
# so no contact address is hardcoded here - this neutral one is only the
# fallback for a caller that doesn't pass one.
DEFAULT_USER_AGENT = "felt-core/0.1 (personal assistant)"
# Chorus lines: the most-repeated line, plus any other line repeated
# nearly as often (a chorus is usually several lines that all repeat
# together, not one).
_CHORUS_COUNT_SLACK = 1
_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9]+$")

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


def with_end_times(lines: list[dict]) -> list[dict]:
    """Pure: parse_lrc's [{"time_ms", "text"}] -> [{"start_ms", "end_ms",
    "text"}], each line ending where the next one starts (the last one
    gets _FINAL_LINE_WINDOW_MS). The real per-line timings a desktop
    client needs to build a mouth-along viseme timeline."""
    out = []
    for i, line in enumerate(lines):
        end_ms = lines[i + 1]["time_ms"] if i + 1 < len(lines) else line["time_ms"] + _FINAL_LINE_WINDOW_MS
        out.append({"start_ms": line["time_ms"], "end_ms": end_ms, "text": line["text"]})
    return out


def find_chorus(lines: list[dict], position_ms: int | None = None) -> dict | None:
    """Pure: the chorus of a synced lyric - the most-repeated non-empty
    line (>= _MIN_CHORUS_REPEATS occurrences), plus any other line
    repeated nearly as often (within _CHORUS_COUNT_SLACK), since a chorus
    is usually several lines. Returns

        {"text", "start_ms", "end_ms", "windows": [...]}

    `windows` lists EVERY occurrence of the chorus, adjacent chorus lines
    merged into one window: {"start_ms", "end_ms", "lines": [{"start_ms",
    "end_ms", "text"}, ...]}. The top-level start_ms/end_ms (kept for
    older clients that only read those) are the window containing
    `position_ms`, else the next one after it, else the first - so a
    client that only checks "am I inside [start_ms, end_ms]?" is right
    for every chorus, not just the first. `text` is the most-repeated
    line. None if nothing repeats often enough to call a chorus."""
    counts: dict[str, int] = {}
    for line in lines:
        if line["text"]:
            counts[line["text"]] = counts.get(line["text"], 0) + 1
    if not counts:
        return None

    chorus_text, top = max(counts.items(), key=lambda item: item[1])
    if top < _MIN_CHORUS_REPEATS:
        return None
    chorus_lines = {
        text for text, count in counts.items() if count >= max(_MIN_CHORUS_REPEATS, top - _CHORUS_COUNT_SLACK)
    }

    windows: list[dict] = []
    for timed_line in with_end_times(lines):
        if timed_line["text"] not in chorus_lines:
            continue
        if windows and windows[-1]["end_ms"] == timed_line["start_ms"]:
            windows[-1]["end_ms"] = timed_line["end_ms"]
            windows[-1]["lines"].append(timed_line)
        else:
            windows.append({"start_ms": timed_line["start_ms"], "end_ms": timed_line["end_ms"], "lines": [timed_line]})

    pick = windows[0]
    if position_ms is not None:
        pick = next(
            (w for w in windows if position_ms <= w["end_ms"]),
            windows[0],
        )
    return {"text": chorus_text, "start_ms": pick["start_ms"], "end_ms": pick["end_ms"], "windows": windows}


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


def _cache_key(track_id: str | None, track_name: str, artist_name: str, album_name: str, duration_s: int) -> str:
    """A Spotify catalogue id (base62) is used as-is. Anything else - a
    local file's null id, a uri with colons - would either collide
    ("None.json" shared by every local file) or not be filesystem-safe,
    so it's keyed by a hash of what was actually looked up instead."""
    if track_id and _TRACK_ID_RE.match(track_id):
        return track_id
    digest = hashlib.sha1(f"{track_name}|{artist_name}|{album_name}|{duration_s}".encode("utf-8")).hexdigest()
    return f"k_{digest[:24]}"


def _cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.json"


async def get_lyrics(
    client: httpx.AsyncClient,
    cache_dir: Path,
    track_id: str | None,
    track_name: str,
    artist_name: str,
    album_name: str,
    duration_s: int,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict | None:
    """`{"synced": [...], "plain": str | None}`, or None if LRCLIB has
    nothing for this track. Cached permanently on disk, one JSON file per
    track_id (`cache_dir/<track_id>.json`) - lyrics don't change for a
    given track, so this survives a felt-core restart, unlike
    get_currently_playing's short in-process TTL. A genuine "not found"
    IS cached (so a track LRCLIB has nothing for isn't re-requested every
    time it plays). Only a real answer is cacheable: a 200 (with or
    without lyrics in it) or a 404. A network failure, a 429 rate limit
    or a 5xx is deliberately NOT cached - none of those mean "no lyrics
    exist", and caching them would hide the track's lyrics forever."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, _cache_key(track_id, track_name, artist_name, album_name, duration_s))
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
            headers={"User-Agent": user_agent or DEFAULT_USER_AGENT},
        )
        if response.status_code == 200:
            body = response.json()
            synced_raw = body.get("syncedLyrics")
            plain = body.get("plainLyrics")
            if synced_raw:
                result = {"synced": parse_lrc(synced_raw), "plain": plain}
            elif plain:
                result = {"synced": [], "plain": plain}
        elif response.status_code != 404:
            # 404 is LRCLIB's normal "not found" (cached as a real miss
            # below). Anything else - 429, 5xx - is transient.
            logger.warning("LRCLIB returned %s for track_id=%s - not caching", response.status_code, track_id)
            return None
    except httpx.HTTPError:
        logger.warning("LRCLIB request failed for track_id=%s", track_id, exc_info=True)
        return None

    path.write_text(json.dumps({"result": result}), encoding="utf-8")
    return result
