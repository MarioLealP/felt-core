"""End-to-end chat check: real messages through chat() with the real felt-llm,
everything else faked and recorded - what does she send to Spotify (or
anywhere else) for each thing Mario says?

Runs inside the felt-core container (felt-llm is only reachable there), on a
throwaway database, so his history and DJ state stay untouched:

    docker cp tools/e2e_chat.py felt-core:/tmp/ && \
    docker exec felt-core /app/.venv/bin/python /tmp/e2e_chat.py [--repeat N] [--only TEXT]

Exit code 1 if any case does something it shouldn't.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import tempfile
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx

from felt_core.brain.orchestrator import chat
from felt_core.config import load_config

LLM_HOSTS = {"felt-llm"}
FAKE_URI = "spotify:track:{:022d}"


@dataclass
class Case:
    say: str
    # the Spotify writes expected, e.g. "PUT /me/player/pause"; None = any
    writes: list[str] | None
    # writes that must not happen (checked when `writes` is None)
    never: list[str] = field(default_factory=list)
    note: str = ""


ANY_PLAYER_WRITE = ["POST /me/player/", "PUT /me/player/"]
PLAY_NOW = ["POST /me/player/queue", "POST /me/player/next"]

CASES: list[Case] = [
    # fixed commands
    Case("skip", ["POST /me/player/next"]),
    Case("can u pause the song?", ["PUT /me/player/pause"]),
    Case("resume", ["PUT /me/player/play"]),
    Case("restart this", ["PUT /me/player/seek"]),
    Case("volume 30", ["PUT /me/player/volume"]),
    Case("turn it up", ["PUT /me/player/volume"]),
    Case("save this song", ["PUT /me/library"]),
    Case("what's this song?", []),
    # play by name - one queue, then nexts up to it
    Case("can you play birdbrain jamie paige", PLAY_NOW),
    Case("play some daft punk", PLAY_NOW),
    # queue only - nothing skipped
    Case("queue birdbrain", ["POST /me/player/queue"]),
    Case("add one more time by daft punk to the queue", ["POST /me/player/queue"]),
    Case("play crossfaith next", ["POST /me/player/queue"]),
    Case(
        "can you stick some zutomayo in the queue for later?", ["POST /me/player/queue"], note="llm"
    ),
    # said loosely - felt-llm picks the tool
    Case("could you throw on some bring me the horizon", PLAY_NOW, note="llm"),
    Case("ugh this song sucks, next", ["POST /me/player/next"], note="llm"),
    Case("hold the music a sec", ["PUT /me/player/pause"], note="llm"),
    Case("it's way too loud", ["PUT /me/player/volume"]),
    Case("ugh it's too quiet", ["PUT /me/player/volume"]),
    Case("can you make it a bit quieter", ["PUT /me/player/volume"], note="llm"),
    # DJ, in order (state carries over)
    Case("be my DJ", None, never=["PUT /me/player/pause"], note="dj"),
    Case("less like this", ["POST /me/player/next"], note="dj"),  # skips + learns
    Case("more like this", [], note="dj"),
    Case("not this one", ["POST /me/player/next"], note="dj"),  # skips + learns
    Case("dj mode off", [], note="dj"),
    # not music at all
    # with music on, pausing is a fair reading; anything more isn't
    Case("stop it", None, never=["POST /me/player/", "PUT /me/player/volume"]),
    Case("what's this song about?", None, never=ANY_PLAYER_WRITE),
    Case("play a game with me", None, never=ANY_PLAYER_WRITE),
    Case("I skipped breakfast", None, never=ANY_PLAYER_WRITE),
]


class Recorder:
    """Fakes every non-LLM host; records each call."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.queue: list[str] = []
        self.playing = True
        self._n = 0

    def _track(self, name: str, artist: str = "Fake Artist") -> dict:
        self._n += 1
        uri = FAKE_URI.format(self._n)
        return {
            "uri": uri,
            "id": uri.rsplit(":", 1)[1],
            "name": name,
            "artists": [{"name": artist}],
            "duration_ms": 200_000,
        }

    def respond(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        query = parse_qs(request.url.query.decode())
        if host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
        if host != "api.spotify.com":
            self.calls.append(f"{request.method} {host}{path}")
            return httpx.Response(503)
        path = path.removeprefix("/v1")
        self.calls.append(f"{request.method} {path}")
        if path == "/me/player/pause":
            self.playing = False
        elif path in ("/me/player/play", "/me/player/next"):
            self.playing = True
            if path == "/me/player/next" and self.queue:
                self.queue.pop(0)
        if path == "/search":
            q = query.get("q", [""])[0]
            m = re.match(r'artist:"(.+)"', q)
            items = (
                [self._track(f"{m[1]} song {i}", m[1]) for i in range(3)]
                if m
                else [self._track(q, q)]
            )
            return httpx.Response(200, json={"tracks": {"items": items}})
        if path == "/me/player/queue":
            if request.method == "POST":
                self.queue.append(query.get("uri", [""])[0])
                return httpx.Response(204)
            return httpx.Response(200, json={"queue": [{"uri": u} for u in self.queue]})
        if path in ("/me/player", "/me/player/currently-playing"):
            item = {
                "uri": FAKE_URI.format(0),
                "id": "0" * 22,
                "name": "Now Song",
                "artists": [{"name": "Now Artist"}],
                "duration_ms": 200_000,
            }
            return httpx.Response(
                200,
                json={
                    "is_playing": self.playing,
                    "progress_ms": 1000,
                    "item": item,
                    "device": {"volume_percent": 50},
                },
            )
        if path == "/me/library/contains":
            return httpx.Response(200, json=[False])
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(204)


def _install(recorder: Recorder) -> None:
    real = httpx.AsyncHTTPTransport.handle_async_request

    async def handle(self, request):
        if request.url.host in LLM_HOSTS:
            return await real(self, request)
        response = recorder.respond(request)
        await request.aread()
        return response

    httpx.AsyncHTTPTransport.handle_async_request = handle  # type: ignore[method-assign]


class _SkillLog(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        msg = record.getMessage()
        if "skill call" in msg or "fast path" in msg or "claimed" in msg:
            self.lines.append(msg)


def _writes(calls: list[str]) -> list[str]:
    return [c for c in calls if not c.startswith("GET ")]


def _verdict(case: Case, writes: list[str]) -> bool:
    stripped = [w.split("?")[0] for w in writes]
    if case.writes is not None:
        return set(stripped) == set(case.writes) and (
            not case.writes or len(stripped) >= len(case.writes)
        )
    return not any(w.startswith(n) for w in stripped for n in case.never)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--only")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="felt-e2e-")
    config = load_config().model_copy(update={"db_path": f"{tmp}/felt.db"})
    recorder = Recorder()
    _install(recorder)
    log = _SkillLog()
    logging.getLogger("felt_core").addHandler(log)
    logging.getLogger("felt_core").setLevel(logging.INFO)

    cases = [c for c in CASES if not args.only or args.only.lower() in c.say.lower()]
    failed = 0
    for run in range(args.repeat):
        for case in cases:
            recorder.calls.clear()
            log.lines.clear()
            try:
                result = await chat(case.say, config, session_id=f"e2e-{run}")
                reply = result.reply
            except Exception as e:  # noqa: BLE001 - a crash is a result here
                reply = f"CRASH {type(e).__name__}: {e}"
            writes = _writes(recorder.calls)
            ok = _verdict(case, writes) and not reply.startswith("CRASH")
            failed += not ok
            print(
                json.dumps(
                    {
                        "ok": ok,
                        "say": case.say,
                        "route": log.lines,
                        "spotify": writes,
                        "other": [
                            c
                            for c in recorder.calls
                            if " api." not in c
                            and "/" in c.split(" ", 1)[1]
                            and not c.split(" ", 1)[1].startswith("/")
                        ],
                        "reply": reply[:160],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    print(f"{len(cases) * args.repeat - failed}/{len(cases) * args.repeat} ok", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
