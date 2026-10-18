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
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx

from felt_core.brain import router
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
    # lookups: one of these skills has to be called (a reply from memory fails)
    skills: tuple[str, ...] = ()


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
    Case("less llike this", ["POST /me/player/next"], note="dj"),  # Mario's typo, live
    Case("play birdbrain by jamie paige", PLAY_NOW, note="dj"),  # mid-set: an interlude
    Case("more like this", [], note="dj"),
    Case("not this one", ["POST /me/player/next"], note="dj"),  # skips + learns
    Case("dj mode off", [], note="dj"),
    # not music at all
    # with music on, pausing is a fair reading; anything more isn't
    Case("stop it", None, never=["POST /me/player/", "PUT /me/player/volume"]),
    Case("what's this song about?", None, never=ANY_PLAYER_WRITE),
    Case("play a game with me", None, never=ANY_PLAYER_WRITE),
    Case("I skipped breakfast", None, never=ANY_PLAYER_WRITE),
    # the phone, through the Home Assistant app (fake - the real one never rings)
    Case("set a timer for 10 minutes", ["POST /phone/SET_TIMER"], note="phone"),
    Case("can you time 25 min for my pasta", ["POST /phone/SET_TIMER"], note="phone"),
    Case("wake me up at 7:30", ["POST /phone/SET_ALARM"], note="phone"),
    Case("set an alarm for 6am tomorrow", ["POST /phone/SET_ALARM"], note="phone"),
    Case("where's my phone", ["POST /phone/NOTIFY"], note="phone"),
    Case("ring my phone pls", ["POST /phone/NOTIFY"], note="phone"),
    Case("how much battery does my phone have?", [], [], "phone", ("get_phone_battery",)),
    Case("when's my alarm?", [], [], "phone", ("list_alarms",)),
    Case("what time is my alarm set for tomorrow", [], [], "phone", ("list_alarms",)),
    # reminders -> calendar events the phone notifies for (WP-46)
    Case("remind me friday at 3pm to call the dentist", ["POST /calendar/reminder"], note="remind"),
    # short and relative: a labelled phone timer rings just the same
    Case(
        "remind me in 20 minutes to check the oven",
        None,
        [],
        "remind",
        ("set_reminder", "set_timer"),
    ),
    Case(
        "every monday at 9 remind me to take the bins out",
        ["POST /calendar/reminder weekly"],
        note="remind",
    ),
    Case(
        "remind me every day at 10pm to take my meds",
        ["POST /calendar/reminder daily"],
        note="remind",
    ),
    Case("add a dentist appointment friday at 3pm", ["POST /calendar/event"], note="remind"),
    # live 2026-10-18, typos and all - she said "I've set that reminder" and set nothing
    Case(
        "set an reminder for 21th to check on teh semantic router",
        ["POST /calendar/reminder"],
        note="remind",
    ),
    Case("set a timer for 5 minutes", ["POST /phone/SET_TIMER"], note="remind"),
    # lookups (cm-hry): Mario's two live misses from 2026-10-07 + kin
    Case("what does the Chaos Orb do in poe2", None, ANY_PLAYER_WRITE, "look", ("poe2_wiki",)),
    Case(
        "how does the gemling legionnaire work in poe 2",
        None,
        ANY_PLAYER_WRITE,
        "look",
        ("poe2_wiki",),
    ),
    Case("my cv on google drive", None, ANY_PLAYER_WRITE, "look", ("search_documents",)),
    Case("find my cv in my documents", None, ANY_PLAYER_WRITE, "look", ("search_documents",)),
    Case("what's the weather like tomorrow?", None, ANY_PLAYER_WRITE, "look", ("get_weather",)),
    Case(
        "who won the last f1 race?",
        None,
        ANY_PLAYER_WRITE,
        "look",
        ("web_search", "news_digest"),
    ),
    Case(
        "is anything down on the server?",
        None,
        ANY_PLAYER_WRITE,
        "look",
        ("get_container_status", "get_recent_alerts"),
    ),
]


class Recorder:
    """Fakes every non-LLM host; records each call."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.queue: list[str] = []
        self.playing = True
        self._n = 0
        self.known: dict[str, dict] = {}
        self.ha_host = ""
        self.now = {
            "uri": FAKE_URI.format(0),
            "id": "0" * 22,
            "name": "Now Song",
            "artists": [{"name": "Now Artist"}],
            "duration_ms": 200_000,
        }

    def _track(self, name: str, artist: str = "Fake Artist") -> dict:
        self._n += 1
        uri = FAKE_URI.format(self._n)
        track = {
            "uri": uri,
            "id": uri.rsplit(":", 1)[1],
            "name": name,
            "artists": [{"name": artist}],
            "duration_ms": 200_000,
        }
        self.known[uri] = track
        return track

    def _ha(self, request: httpx.Request, path: str) -> httpx.Response:
        """Home Assistant: the phone (companion app) part - recorded as
        'POST /phone/SET_TIMER' etc., never reaching the real phone."""
        if path.startswith("/api/services/notify/mobile_app_"):
            body = json.loads(request.content or b"{}")
            action = (body.get("data") or {}).get("intent_action", "")
            what = action.rsplit(".", 1)[-1] if action else "NOTIFY"
            self.calls.append(f"POST /phone/{what}")
            return httpx.Response(200, json=[])
        if path.endswith("_battery_level"):
            self.calls.append(f"GET /phone{path}")
            return httpx.Response(200, json={"state": "64"})
        if path.endswith("_next_alarm"):
            self.calls.append(f"GET /phone{path}")
            return httpx.Response(200, json={"state": "2026-10-19T05:30:00+00:00"})
        if path.endswith("_battery_state"):
            return httpx.Response(200, json={"state": "charging"})
        self.calls.append(f"{request.method} /ha{path}")
        return httpx.Response(503)

    def _calendar(self, request: httpx.Request, path: str) -> httpx.Response:
        """Google Calendar: one primary calendar; a created event is recorded
        as 'POST /calendar/event' (+ ' weekly' etc. when it repeats)."""
        if path == "/users/me/calendarList":
            cal = {"id": "primary-id", "summary": "Mario", "primary": True, "accessRole": "owner"}
            return httpx.Response(200, json={"items": [cal]})
        if request.method == "POST" and path.endswith("/events"):
            body = json.loads(request.content or b"{}")
            rule = (body.get("recurrence") or [""])[0]
            freq = re.search(r"FREQ=(\w+)", rule)
            tag = "reminder" if body.get("extendedProperties") else "event"
            repeat = f" {freq[1].lower()}" if freq else ""
            self.calls.append(f"POST /calendar/{tag}{repeat}")
            return httpx.Response(200, json={"id": "new"})
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        self.calls.append(f"{request.method} /calendar{path}")
        return httpx.Response(503)

    def respond(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        query = parse_qs(request.url.query.decode())
        if host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
        if host == self.ha_host:
            return self._ha(request, path)
        if host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
        if host == "www.googleapis.com" and path.startswith("/calendar/v3"):
            return self._calendar(request, path.removeprefix("/calendar/v3"))
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
                uri = self.queue.pop(0)
                self.now = self.known.get(uri, {**self.now, "uri": uri, "name": uri})
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
            item = self.now
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
        if "skill call" in msg or "fast path" in msg or "claimed" in msg or "router shadow" in msg:
            self.lines.append(msg)


def _writes(calls: list[str]) -> list[str]:
    return [c for c in calls if not c.startswith("GET ")]


def _verdict(case: Case, writes: list[str], route: list[str]) -> bool:
    if case.skills and not any(f"skill call: {k}" in r for r in route for k in case.skills):
        return False
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
    ap.add_argument("--note", help="only cases with this note, e.g. look / dj / llm")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="felt-e2e-")
    config = load_config().model_copy(update={"db_path": f"{tmp}/felt.db"})
    # the semantic router in shadow mode (cm-6b0): the image's model, or a
    # one-off copy for a run before it's deployed
    if not os.path.isdir(config.router_model_dir):
        from fastembed import TextEmbedding

        config = config.model_copy(update={"router_model_dir": "/tmp/e2e-router-model"})
        await asyncio.to_thread(
            TextEmbedding, config.router_model, cache_dir=config.router_model_dir
        )
    await router.warm(config)
    recorder = Recorder()
    recorder.ha_host = httpx.URL(config.ha_url or "http://-").host
    _install(recorder)
    log = _SkillLog()
    logging.getLogger("felt_core").addHandler(log)
    logging.getLogger("felt_core").setLevel(logging.INFO)

    cases = [
        c
        for c in CASES
        if (not args.only or args.only.lower() in c.say.lower())
        and (not args.note or c.note == args.note)
    ]
    failed = 0
    for run in range(args.repeat):
        for i, case in enumerate(cases):
            recorder.calls.clear()
            log.lines.clear()
            try:
                # a conversation per case: earlier "couldn't reach" replies in
                # the history taught the model not to try (DJ state isn't
                # per-session, so the DJ cases still carry over)
                result = await chat(case.say, config, session_id=f"e2e-{run}-{i}")
                reply = result.reply
                await router.drain()
            except Exception as e:  # noqa: BLE001 - a crash is a result here
                reply = f"CRASH {type(e).__name__}: {e}"
            writes = _writes(recorder.calls)
            ok = _verdict(case, writes, log.lines) and not reply.startswith("CRASH")
            if case.skills and ok and any(" /" not in c for c in recorder.calls):
                # a service she called was down (all faked): she has to say
                # so, not answer anyway
                ok = bool(
                    re.search(
                        r"couldn'?t|can'?t|unable|down|trouble|hard time"
                        r"|not (?:able|working|responding|getting|seeing)",
                        reply,
                        re.I,
                    )
                )
            failed += not ok
            print(
                json.dumps(
                    {
                        "ok": ok,
                        "say": case.say,
                        "route": log.lines,
                        "spotify": writes,
                        "other": [c for c in recorder.calls if " /" not in c],
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
