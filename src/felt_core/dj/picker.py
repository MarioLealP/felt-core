"""Her picks. Familiar ones come straight from his listening history (songs
he plays through, never the last few days', never ones he skips); new ones
are artists he doesn't listen to yet, named by felt-llm with a "because",
with real tracks from Spotify. The model only names artists - a local model
knows artists far better than exact song titles (2026-10-17 dry run: of nine
suggested titles, one existed)."""

import datetime as dt
import json
import logging
import random
import re

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
        "artists": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "because": {"type": "string"},
                },
                "required": ["name", "because"],
            },
        }
    },
    "required": ["artists"],
}
_RECENT_PICK_DAYS = 7
_RECENT_PLAY_DAYS = 3
_TRACKS_PER_NEW_ARTIST = 5  # pick among an artist's top few: a good first impression
_top_cache: dict = {"artists": [], "at": 0.0}
# a catalogue track id; local files show up as "spotify:local:..." and can't be queued
_TRACK_ID = re.compile(r"^[0-9A-Za-z]{22}$")


async def _top_artists(client: httpx.AsyncClient, config: FeltConfig) -> list[str]:
    now = dt.datetime.now().timestamp()
    if now - _top_cache["at"] > 24 * 3600:
        try:
            _top_cache["artists"] = await sp.top_artists(client, config)
            _top_cache["at"] = now
        except httpx.HTTPError:
            logger.info("dj: no top artists from Spotify right now")
    return _top_cache["artists"]


def _recent(last: str | None, days: int) -> bool:
    if not last:
        return False
    try:
        when = dt.datetime.fromisoformat(last)
    except ValueError:
        return False
    # music_taste stamps with SQLite's datetime('now'): UTC, no zone
    return dt.datetime.now(dt.UTC).replace(tzinfo=None) - when < dt.timedelta(days=days)


def familiar(history: dict[str, dict], count: int, avoid: set[str]) -> list[dict]:
    """Pure-ish (random): songs he plays through, weighted by how often;
    never one he mostly skips, played in the last few days, or in `avoid`."""
    pool = [
        (tid, t)
        for tid, t in history.items()
        if _TRACK_ID.match(tid or "")
        and t["artist"]
        and t["plays"] > t["skips"]
        and t["skips"] / t["plays"] < 0.5
        and not _recent(t["last"], _RECENT_PLAY_DAYS)
        and f"spotify:track:{tid}" not in avoid
    ]
    picks: list[dict] = []
    while pool and len(picks) < count:
        weights = [t["plays"] - t["skips"] for _, t in pool]
        i = random.choices(range(len(pool)), weights=weights)[0]
        tid, t = pool.pop(i)
        picks.append(
            {
                "uri": f"spotify:track:{tid}",
                "title": t["title"],
                "artist": t["artist"],
                "discovery": False,
            }
        )
    return picks


async def _new_artists(config: FeltConfig, profile: str, known: set[str], count: int) -> list[dict]:
    system = (
        "You are Felt, DJ-ing for Mario on Spotify. Name real artists he doesn't listen to yet "
        "who fit his taste below - exact artist names as on Spotify, each with a short reason "
        "tied to something he likes ('because' - e.g. 'like Jonathan Young, metal covers of game "
        "songs'). Vary them; none from his lists."
    )
    user = f"{profile}\n\nName {count} artists new to him. Answer as JSON."
    reply = await local_llm.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=500,
        temperature=0.9,
        json_schema=_SCHEMA,
        config=config,
    )
    try:
        artists = json.loads(reply.text).get("artists", [])
    except (ValueError, AttributeError):
        return []
    return [
        a
        for a in artists
        if isinstance(a, dict) and a.get("name") and sp.norm_name(a["name"]) not in known
    ]


async def discoveries(
    client: httpx.AsyncClient,
    config: FeltConfig,
    profile: str,
    known: set[str],
    count: int,
    avoid: set[str],
) -> list[dict]:
    """`count` tracks by artists new to him (one per artist), from Spotify."""
    picks: list[dict] = []
    for a in await _new_artists(config, profile, known, count * 2):
        if len(picks) >= count:
            break
        try:
            tracks = await sp.artist_tracks(client, config, a["name"])
        except httpx.HTTPError:
            continue
        tracks = [t for t in tracks[:_TRACKS_PER_NEW_ARTIST] if t["uri"] not in avoid]
        if not tracks:
            logger.info("dj: no tracks for suggested artist %r", a["name"])
            continue
        t = random.choice(tracks)
        avoid.add(t["uri"])
        picks.append(
            {
                "uri": t["uri"],
                "title": t["name"],
                "artist": ", ".join(x["name"] for x in t.get("artists", [])),
                "discovery": True,
                "because": str(a.get("because", ""))[:160],
            }
        )
    return picks


async def pick(
    client: httpx.AsyncClient, config: FeltConfig, count: int, avoid: set[str]
) -> list[dict]:
    """Up to `count` picks {uri, title, artist, discovery[, because]}, about
    discovery_share of them by artists new to him."""
    db = config.db_path
    share = learning.discovery_share(store.discovery_outcomes(db, 10))
    want_new = sum(random.random() < share for _ in range(count))
    avoid = avoid | store.recent_pick_uris(db, _RECENT_PICK_DAYS)
    history = store.history_tracks(db)
    picks = familiar(history, count - want_new, avoid)
    want_new = count - len(picks)
    if want_new:
        artists = store.history_artists(db)
        top = await _top_artists(client, config)
        known = {sp.norm_name(a["artist"]) for a in artists} | {sp.norm_name(a) for a in top}
        profile = build_profile(
            artists,
            top,
            store.feedback(db, 20),
            store.pick_outcomes(db, 12),
            dt.datetime.now().hour,
        )
        avoid = avoid | {p["uri"] for p in picks}
        picks += await discoveries(client, config, profile, known, want_new, avoid)
    if len(picks) < count:  # too few discoveries came through: familiar ones fill in
        taken = avoid | {p["uri"] for p in picks}
        picks += familiar(history, count - len(picks), taken)
    random.shuffle(picks)
    return picks
