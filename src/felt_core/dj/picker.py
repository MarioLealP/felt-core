"""Her picks: felt-llm suggests songs from his taste profile, Spotify confirms
each one exists (by that artist), and the mix of new-to-him vs familiar is
enforced here, not left to the model's word."""

import datetime as dt
import json
import logging
import random

import httpx

from felt_core import local_llm
from felt_core import spotify_client as sp
from felt_core.config import FeltConfig
from felt_core.dj import learning, store
from felt_core.dj.profile import build_profile

logger = logging.getLogger(__name__)

_SCHEMA = {
    "type": "object",
    "properties": {
        "songs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "artist": {"type": "string"},
                    "new": {"type": "boolean"},
                },
                "required": ["title", "artist", "new"],
            },
        }
    },
    "required": ["songs"],
}
_RECENT_PICK_DAYS = 7
_RECENT_PLAY_DAYS = 3
_top_cache: dict = {"artists": [], "at": 0.0}


async def _top_artists(client: httpx.AsyncClient, config: FeltConfig) -> list[str]:
    now = dt.datetime.now().timestamp()
    if now - _top_cache["at"] > 24 * 3600:
        try:
            _top_cache["artists"] = await sp.top_artists(client, config)
            _top_cache["at"] = now
        except httpx.HTTPError:
            logger.info("dj: no top artists from Spotify right now")
    return _top_cache["artists"]


async def _suggest(config: FeltConfig, profile: str, count: int, new: int) -> list[dict]:
    system = (
        "You are Felt, DJ-ing for Mario on Spotify. Suggest real, existing songs that fit his "
        "taste below - exact song titles and artist names as they appear on Spotify. Mark a "
        "song new=true when it's likely new to him: an artist he doesn't play, or a deeper cut "
        "he hasn't heard; new songs should still fit what he likes. Never suggest the same song "
        "twice, and vary the artists."
    )
    user = f"{profile}\n\nSuggest {count} songs, at least {new} of them new to him. Answer as JSON."
    reply = await local_llm.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=900,
        temperature=0.9,
        json_schema=_SCHEMA,
        config=config,
    )
    try:
        songs = json.loads(reply.text).get("songs", [])
    except (ValueError, AttributeError):
        return []
    return [s for s in songs if isinstance(s, dict) and s.get("title") and s.get("artist")]


def _recent(last: str | None, days: int) -> bool:
    if not last:
        return False
    try:
        when = dt.datetime.fromisoformat(last)
    except ValueError:
        return False
    # music_taste stamps with SQLite's datetime('now'): UTC, no zone
    return dt.datetime.now(dt.UTC).replace(tzinfo=None) - when < dt.timedelta(days=days)


async def pick(
    client: httpx.AsyncClient, config: FeltConfig, count: int, avoid: set[str]
) -> list[dict]:
    """Up to `count` playable picks {uri, title, artist, discovery}, none in
    `avoid` (what's queued now) or recently picked/played, none he usually
    skips; about discovery_share of them new to him."""
    db = config.db_path
    share = learning.discovery_share(store.discovery_outcomes(db, 10))
    want_new = sum(random.random() < share for _ in range(count))
    profile = build_profile(
        store.history_artists(db),
        await _top_artists(client, config),
        store.feedback(db, 20),
        store.pick_outcomes(db, 12),
        dt.datetime.now().hour,
    )
    suggestions = await _suggest(config, profile, count * 3, max(want_new, 1))
    history = store.history_tracks(db)
    skip_uris = avoid | store.recent_pick_uris(db, _RECENT_PICK_DAYS)
    found: list[dict] = []
    for s in suggestions:
        try:
            track = await sp.find_track(client, config, s["title"], s["artist"])
        except httpx.HTTPError:
            continue
        if track is None or track["uri"] in skip_uris:
            continue
        seen = history.get(track.get("id", ""))
        if seen and (
            _recent(seen["last"], _RECENT_PLAY_DAYS)
            or (seen["plays"] >= 2 and seen["skips"] / seen["plays"] >= 0.5)
        ):
            continue
        skip_uris.add(track["uri"])
        found.append(
            {
                "uri": track["uri"],
                "title": track["name"],
                "artist": ", ".join(a["name"] for a in track.get("artists", [])),
                "discovery": seen is None,
            }
        )
    # "new" = never in his history AND not saved in his library
    unknown = [f["uri"] for f in found if f["discovery"]]
    try:
        saved = dict(zip(unknown, await sp.library_contains(client, config, unknown), strict=False))
    except httpx.HTTPError:
        saved = {}
    for f in found:
        f["discovery"] = f["discovery"] and not saved.get(f["uri"], False)
    return choose(found, count, want_new)


def choose(found: list[dict], count: int, want_new: int) -> list[dict]:
    """Pure: `count` picks with `want_new` discoveries where there are enough
    of each, filled from the other kind otherwise; shuffled together."""
    new = [f for f in found if f["discovery"]]
    known = [f for f in found if not f["discovery"]]
    n_new = min(want_new, len(new))
    n_known = min(count - n_new, len(known))
    n_new = min(count - n_known, len(new))
    picks = new[:n_new] + known[:n_known]
    random.shuffle(picks)
    return picks
