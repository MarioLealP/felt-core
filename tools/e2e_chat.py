"""End-to-end chat check: real messages through chat() with the real felt-llm,
everything else faked and recorded - what does she send to Spotify (or
anywhere else) for each thing Mario says?

Runs inside the felt-core container (felt-llm is only reachable there), on a
throwaway database, so his history and DJ state stay untouched:

    docker cp tools/e2e_chat.py felt-core:/tmp/ && \
    docker exec felt-core /app/.venv/bin/python /tmp/e2e_chat.py [--repeat N] [--only TEXT]

`--stream` (WP-65) runs every message through chat_streamed() instead: the
same checks, plus the chunks have to add up to the reply, and each line
gets the time to her first chunk.

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
import time
from urllib.parse import parse_qs

import httpx2
from pydantic import BaseModel, Field

from felt_core.brain import router
from felt_core.brain.orchestrator import chat, chat_streamed
from felt_core.brain.result import ChatResult
from felt_core.clients import ha_calendar
from felt_core.config import load_config
from felt_core.core import appointment_hold, features


def _llm_hosts() -> set[str]:
    """The models are real; everything else is faked. Their hosts come from her settings, so
    a model moved to a fixed address (felt-vision on k3s: FELT_VISION_URL=http://10.43.0.71)
    stays real.
    """
    config = load_config()
    urls = (config.local_llm_url, config.vision_url)
    return {"felt-llm", "felt-vision"} | {httpx2.URL(u).host for u in urls if u}


LLM_HOSTS = _llm_hosts()
FAKE_URI = "spotify:track:{:022d}"


class Case(BaseModel):
    say: str
    # the Spotify writes expected, e.g. "PUT /me/player/pause"; None = any
    writes: list[str] | None
    # writes that must not happen (checked when `writes` is None)
    never: list[str] = Field(default_factory=list)
    note: str = ""
    # lookups: one of these skills has to be called (a reply from memory fails)
    skills: tuple[str, ...] = ()
    # a second message in the same conversation (her follow-up question's answer)
    then: str | None = None
    # calendar cases: the appointment that has to be there at the end, with a
    # place (True) or without (False)
    place: bool | None = None
    # calendar cases: words every created event's summary must keep (cm-zha)
    summary_has: tuple[str, ...] = ()


ANY_PLAYER_WRITE = ["POST /me/player/", "PUT /me/player/"]
HOME_WRITES = ["POST /ha-assist", "POST /home/"]

# Home Assistant's built-in matcher (ha_assist.py), as the real one answered
# these commands (its debug API, Felt's device in Bedroom, 2026-10-19); any
# other text is "no match"
HA_MATCHES = {
    "turn off the shelf light": "action_done",
    "turn on my shelf light": "action_done",
    "turn off the chandelure": "action_done",
    "turn front left on": "action_done",
    "lights off": "action_done",
    "set the chandelure to 30%": "action_done",
    "turn off the dehumidifier": "action_done",
    "is the fan on": "query_answer",
}
# what the model path's smart_home_control sees (FELT_HA_ENTITIES' states)
# the Ender mid-print (2026-10-19: printer_status reads HA's sensor.ender_*)
ENDER = {
    "state": "printing",
    "file": "hexwall_tile_07.gcode",
    "progress": "62.4",
    "layer": "84 / 136",
    "time_left": "3960",
    "done_at": "2026-10-19T20:40:00+00:00",
    "nozzle": "214.8",
    "nozzle_target": "215.0",
    "bed": "60.1",
    "bed_target": "60.0",
}
HA_DEVICES = {
    "switch.shelf": "Shelf",
    "switch.extraplug": "Dehumidifier Plug",
    "light.chandelure": "Chandelure",
    "light.yeelight_colorb_0x1be53fa1": "Front Left",
    "fan.core_300s_series": "Air Purifier",
}
PLAY_NOW = ["POST /me/player/queue", "POST /me/player/next"]

CASES: list[Case] = [
    # fixed commands
    Case(say="skip", writes=["POST /me/player/next"]),
    Case(say="can u pause the song?", writes=["PUT /me/player/pause"]),
    Case(say="resume", writes=["PUT /me/player/play"]),
    Case(say="restart this", writes=["PUT /me/player/seek"]),
    Case(say="volume 30", writes=["PUT /me/player/volume"]),
    Case(say="turn it up", writes=["PUT /me/player/volume"]),
    Case(say="save this song", writes=["PUT /me/library"]),
    Case(say="what's this song?", writes=[]),
    # play by name - one queue, then nexts up to it
    Case(say="can you play birdbrain jamie paige", writes=PLAY_NOW),
    Case(say="play some daft punk", writes=PLAY_NOW),
    # queue only - nothing skipped
    Case(say="queue birdbrain", writes=["POST /me/player/queue"]),
    Case(say="add one more time by daft punk to the queue", writes=["POST /me/player/queue"]),
    Case(say="play crossfaith next", writes=["POST /me/player/queue"]),
    Case(say="can you stick some zutomayo in the queue for later?", writes=["POST /me/player/queue"], note="llm"),
    # said loosely - felt-llm picks the tool
    Case(say="could you throw on some bring me the horizon", writes=PLAY_NOW, note="llm"),
    Case(say="ugh this song sucks, next", writes=["POST /me/player/next"], note="llm"),
    Case(say="hold the music a sec", writes=["PUT /me/player/pause"], note="llm"),
    Case(say="it's way too loud", writes=["PUT /me/player/volume"]),
    Case(say="ugh it's too quiet", writes=["PUT /me/player/volume"]),
    Case(say="can you make it a bit quieter", writes=["PUT /me/player/volume"], note="llm"),
    # DJ, in order (state carries over)
    Case(say="be my DJ", writes=None, never=["PUT /me/player/pause"], note="dj"),
    Case(say="less like this", writes=["POST /me/player/next"], note="dj"),  # skips + learns
    Case(say="less llike this", writes=["POST /me/player/next"], note="dj"),  # Mario's typo, live
    Case(say="play birdbrain by jamie paige", writes=PLAY_NOW, note="dj"),  # mid-set: an interlude
    Case(say="more like this", writes=[], note="dj"),
    Case(say="not this one", writes=["POST /me/player/next"], note="dj"),  # skips + learns
    Case(say="dj mode off", writes=[], note="dj"),
    # not music at all
    # with music on, pausing is a fair reading; anything more isn't
    Case(say="stop it", writes=None, never=["POST /me/player/", "PUT /me/player/volume"]),
    Case(say="what's this song about?", writes=None, never=ANY_PLAYER_WRITE),
    Case(say="play a game with me", writes=None, never=ANY_PLAYER_WRITE),
    Case(say="I skipped breakfast", writes=None, never=ANY_PLAYER_WRITE),
    # the phone, through the Home Assistant app (fake - the real one never rings)
    Case(say="set a timer for 10 minutes", writes=["POST /phone/SET_TIMER"], note="phone"),
    Case(say="can you time 25 min for my pasta", writes=["POST /phone/SET_TIMER"], note="phone"),
    Case(say="wake me up at 7:30", writes=["POST /phone/SET_ALARM"], note="phone"),
    Case(say="set an alarm for 6am tomorrow", writes=["POST /phone/SET_ALARM"], note="phone"),
    Case(say="where's my phone", writes=["POST /phone/NOTIFY"], note="phone"),
    Case(say="ring my phone pls", writes=["POST /phone/NOTIFY"], note="phone"),
    Case(say="how much battery does my phone have?", writes=[], never=[], note="phone", skills=("get_phone_battery",)),
    Case(say="when's my alarm?", writes=[], never=[], note="phone", skills=("list_alarms",)),
    Case(say="what time is my alarm set for tomorrow", writes=[], never=[], note="phone", skills=("list_alarms",)),
    # the shopping list: Home Assistant's todo list (fake - the real one stays untouched)
    Case(say="add milk to the shopping list", writes=["POST /todo/add_item"], note="shop"),
    # several at once: her skill, one add each (cm-fmy; HA's matcher made it one item)
    Case(
        say="put eggs and bread on my shopping list",
        writes=["POST /todo/add_item", "POST /todo/add_item"],
        note="shop",
    ),
    Case(
        say="add coffee, oat milk and bananas to the list",
        writes=["POST /todo/add_item", "POST /todo/add_item", "POST /todo/add_item"],
        note="shop",
    ),
    Case(say="we're out of coffee, add it to the list", writes=["POST /todo/add_item"], note="shop"),
    # router shadow 2026-10-18..25: answered in words, nothing added (cm-6b0 act mode)
    Case(say="add baguette to the shopping list", writes=["POST /todo/add_item"], note="shop"),
    Case(say="what's on my shopping list?", writes=[], never=[], note="shop", skills=("shopping_list",)),
    Case(say="take oat milk off the shopping list", writes=["POST /todo/remove_item"], note="shop"),
    # the Ender, read from HA (2026-10-19)
    Case(say="how's the 3d printer doing?", writes=[], never=[], note="printer", skills=("printer_status",)),
    Case(say="is my print done yet?", writes=[], never=[], note="printer", skills=("printer_status",)),
    Case(say="how long left on the ender", writes=[], never=[], note="printer", skills=("printer_status",)),
    # home: plain commands go to HA's matcher (ha_assist), the rest to the model
    Case(say="turn off the shelf light", writes=["POST /ha-assist turn off the shelf light"], note="home"),
    Case(
        say="Felt, turn off the chandelure please",
        writes=["POST /ha-assist turn off the chandelure"],
        note="home",
    ),
    Case(say="lights off", writes=["POST /ha-assist lights off"], note="home"),
    Case(say="turn on my shelf light please :)", writes=["POST /ha-assist turn on my shelf light"], note="home"),
    Case(say="set the chandelure to 30%", writes=["POST /ha-assist set the chandelure to 30%"], note="home"),
    Case(say="turn off the dehumidifier", writes=["POST /ha-assist turn off the dehumidifier"], note="home"),
    Case(say="turn front left on, thanks!", writes=["POST /ha-assist turn front left on"], note="home"),
    Case(say="is the fan on?", writes=[], note="home"),
    # HA doesn't match these: her model and smart_home_control do
    Case(
        say="could you please turn the lights back off",
        writes=None,
        never=[],
        note="home",
        skills=("smart_home_control",),
    ),
    Case(
        say="turn off the shelf light and tell me a joke about it",
        writes=None,
        never=[],
        note="home",
        skills=("smart_home_control",),
    ),
    Case(say="are the lights still on", writes=[], never=[], note="home", skills=("smart_home_control",)),
    # router shadow 2026-10-18..25: no match in HA, then a reply with no tool
    Case(say="lights on", writes=None, never=[], note="home", skills=("smart_home_control",)),
    # dimming (2026-10-19): HA's matcher misses these, her skill dims
    Case(
        say="chandelure to 30 percente please",
        writes=["POST /home/turn_on ['light.chandelure'] 30%"],
        note="home",
    ),
    Case(say="dim the chandelure", writes=["POST /home/turn_on ['light.chandelure'] 30%"], note="home"),
    Case(
        say="dim the lights",
        writes=["POST /home/turn_on ['light.chandelure', 'light.yeelight_colorb_0x1be53fa1'] 30%"],
        note="home",
    ),
    # colour (cm-7gf): HA's matcher misses these too
    Case(
        say="make the chandelure blue",
        writes=["POST /home/turn_on ['light.chandelure'] colour=blue"],
        note="home",
    ),
    Case(
        say="lights warm white please",
        writes=["POST /home/turn_on ['light.chandelure', 'light.yeelight_colorb_0x1be53fa1'] colour=2700"],
        note="home",
    ),
    # not commands: nothing switched
    Case(say="can you turn around?", writes=None, never=HOME_WRITES, note="home"),
    Case(say="is the server on fire?", writes=None, never=HOME_WRITES, note="home"),
    # reminders -> calendar events the phone notifies for (WP-46)
    Case(say="remind me friday at 3pm to call the dentist", writes=["POST /calendar/reminder"], note="remind"),
    # short and relative: a labelled phone timer rings just the same
    Case(
        say="remind me in 20 minutes to check the oven",
        writes=None,
        never=[],
        note="remind",
        skills=("set_reminder", "set_timer"),
    ),
    Case(
        say="every monday at 9 remind me to take the bins out",
        writes=["POST /calendar/reminder weekly"],
        note="remind",
    ),
    Case(
        say="remind me every day at 10pm to take my meds",
        writes=["POST /calendar/reminder daily"],
        note="remind",
    ),
    Case(say="add a dentist appointment friday at 3pm", writes=["POST /calendar/event"], note="remind"),
    # live 2026-10-21: "I can't make an appointment, no calendar access" - a date-only
    # event: all day, no midnight hour, no "where?" (cm-0vl)
    Case(
        say="make an appointment so i know 4th november is WoW forever release",
        writes=["POST /calendar/event all-day"],
        note="remind",
        summary_has=("forever",),
    ),
    # live 2026-10-23: she sent "4 November 15:00" -> Wednesday 3pm and "where is it?"
    Case(
        say="put the 4th of November in my calendar as WoW Forever release",
        writes=["POST /calendar/event all-day"],
        note="remind",
        summary_has=("forever",),
    ),
    Case(say="what's on my calendar tomorrow?", writes=[], never=[], note="remind", skills=("list_calendar_events",)),
    # an appointment's place, Mario's four cases (2026-10-19): no place -> she
    # asks where and holds it; "none" adds it at once, a place adds it with it,
    # no answer adds it after 30 s (the case's end stands in for that)
    Case(
        say="schedule my dentist to tomorrow at 11",
        writes=["POST /calendar/event"],
        note="remind",
        then="none",
        place=False,
    ),
    Case(
        say="schedule my dentist to tomorrow at 11 at Clínica Sorriso in Gaia",
        writes=["POST /calendar/event @place"],
        note="remind",
        place=True,
    ),
    Case(
        say="schedule my dentist to tomorrow at 11",
        writes=["POST /calendar/event @place"],
        note="remind",
        then="it's at Clínica Sorriso in Gaia",
        place=True,
    ),
    Case(
        say="schedule my dentist to tomorrow at 11",
        writes=["POST /calendar/event"],
        note="remind",
        place=False,
    ),
    # live 2026-10-18 ("for 21th"), typos and all - she said "I've set that reminder" and
    # set nothing; a day + month, since a bare day number goes past within days
    Case(
        say="set an reminder for 3rd of decmber to check on teh semantic router",
        writes=["POST /calendar/reminder"],
        note="remind",
    ),
    # live 2026-10-22: 'WoW Forever' became 'World of Warcraft: The War Within' - rewording
    # is fine, but the thing he named stays (Mario: "that was plain wrong") (cm-zha)
    Case(
        say="Add reminder for wow forever release on4th of novemeber",
        writes=["POST /calendar/reminder"],
        note="remind",
        summary_has=("forever",),
    ),
    Case(say="set a timer for 5 minutes", writes=["POST /phone/SET_TIMER"], note="remind"),
    # lookups (cm-hry): Mario's two live misses from 2026-10-07 + kin
    Case(
        say="what does the Chaos Orb do in poe2",
        writes=None,
        never=ANY_PLAYER_WRITE,
        note="look",
        skills=("poe2_wiki",),
    ),
    Case(
        say="how does the gemling legionnaire work in poe 2",
        writes=None,
        never=ANY_PLAYER_WRITE,
        note="look",
        skills=("poe2_wiki",),
    ),
    Case(say="my cv on google drive", writes=None, never=ANY_PLAYER_WRITE, note="look", skills=("search_documents",)),
    Case(
        say="find my cv in my documents", writes=None, never=ANY_PLAYER_WRITE, note="look", skills=("search_documents",)
    ),
    Case(
        say="what's the weather like tomorrow?",
        writes=None,
        never=ANY_PLAYER_WRITE,
        note="look",
        skills=("get_weather",),
    ),
    Case(
        say="who won the last f1 race?",
        writes=None,
        never=ANY_PLAYER_WRITE,
        note="look",
        skills=("web_search", "news_digest"),
    ),
    Case(
        say="is anything down on the server?",
        writes=None,
        never=ANY_PLAYER_WRITE,
        note="look",
        skills=("get_container_status", "get_recent_alerts"),
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
        self.config = load_config()
        # HA's calendar events she created this case
        self.events: list[dict] = []
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

    def _ha(self, request: httpx2.Request, path: str) -> httpx2.Response:
        """Home Assistant: the phone (companion app) part - recorded as
        'POST /phone/SET_TIMER' etc., never reaching the real phone.
        """
        calendar = self._calendar(request, path)
        if calendar is not None:
            return calendar
        if path == "/api/conversation/process":
            # HA's matcher: a match is done there - recorded, never reaching HA
            text = json.loads(request.content or b"{}").get("text", "")
            kind = HA_MATCHES.get(text)
            if kind is None:
                code = {"code": "no_intent_match"}
                return httpx2.Response(200, json={"response": {"response_type": "error", "data": code}})
            self.calls.append(f"{'POST' if kind == 'action_done' else 'GET'} /ha-assist {text}")
            speech = {"plain": {"speech": "Done" if kind == "action_done" else "Yes"}}
            return httpx2.Response(200, json={"response": {"response_type": kind, "speech": speech}})
        if path.startswith("/api/states/") and path.removeprefix("/api/states/") in HA_DEVICES:
            entity = path.removeprefix("/api/states/")
            attrs: dict = {"friendly_name": HA_DEVICES[entity]}
            if entity.startswith("light."):
                # as HA reports the Chandelure and the Yeelights (2026-10-25)
                attrs["supported_color_modes"] = ["color_temp", "hs", "rgb"]
            return httpx2.Response(200, json={"entity_id": entity, "state": "on", "attributes": attrs})
        m = re.match(r"^/api/services/(light|switch|fan)/(turn_on|turn_off|toggle)$", path)
        if m:
            body = json.loads(request.content or b"{}")
            dim = f" {body['brightness_pct']}%" if "brightness_pct" in body else ""
            colour = body.get("color_name") or body.get("color_temp_kelvin")
            colour = f" colour={colour}" if colour else ""
            self.calls.append(f"POST /home/{m[2]} {body.get('entity_id')}{dim}{colour}")
            return httpx2.Response(200, json=[])
        if path.startswith("/api/states/sensor.ender_"):
            field = path.removeprefix("/api/states/sensor.ender_")
            if field not in ENDER:
                return httpx2.Response(404)
            self.calls.append(f"GET /ender/{field}")
            return httpx2.Response(200, json={"state": ENDER[field]})
        if path.startswith("/api/services/notify/mobile_app_"):
            body = json.loads(request.content or b"{}")
            action = (body.get("data") or {}).get("intent_action", "")
            what = action.rsplit(".", 1)[-1] if action else "NOTIFY"
            self.calls.append(f"POST /phone/{what}")
            return httpx2.Response(200, json=[])
        if path.endswith("_battery_level"):
            self.calls.append(f"GET /phone{path}")
            return httpx2.Response(200, json={"state": "64"})
        if path.endswith("_next_alarm"):
            self.calls.append(f"GET /phone{path}")
            return httpx2.Response(200, json={"state": "2026-10-19T05:30:00+00:00"})
        if path.endswith("_battery_state"):
            return httpx2.Response(200, json={"state": "charging"})
        if path.startswith("/api/services/todo/"):
            service = path.rsplit("/", 1)[1]
            if service == "get_items":
                self.calls.append("GET /todo/get_items")
                items = [
                    {"summary": "oat milk", "uid": "a" * 32, "status": "needs_action"},
                    {"summary": "rice", "uid": "b" * 32, "status": "needs_action"},
                ]
                todo = {"todo.shopping_list": {"items": items}}
                return httpx2.Response(200, json={"changed_states": [], "service_response": todo})
            # the item after "?" (the verdict compares the part before it)
            item = json.loads(request.content or b"{}").get("item", "")
            self.calls.append(f"POST /todo/{service}?{item}")
            return httpx2.Response(200, json=[])
        self.calls.append(f"{request.method} /ha{path}")
        return httpx2.Response(503)

    def _calendar(self, request: httpx2.Request, path: str) -> httpx2.Response | None:
        """HA's calendars: Mario's primary and the reminders one, holding only
        what she created this case.
        """
        if path == "/api/calendars":
            cals = [self.config.ha_calendar_default, self.config.ha_calendar_reminders]
            return httpx2.Response(200, json=[{"entity_id": c, "name": c} for c in cals])
        if path.startswith("/api/calendars/"):
            cal = path.removeprefix("/api/calendars/")
            return httpx2.Response(200, json=[e for e in self.events if e["calendar"] == cal])
        if path.startswith("/api/states/calendar."):
            return httpx2.Response(200, json={"attributes": {"supported_features": 3}})
        if path == "/api/services/homeassistant/update_entity":
            return httpx2.Response(200, json=[])
        return None

    async def ws_call(self, config, message: dict) -> dict:
        """ha_calendar's websocket: a created event is recorded as 'POST
        /calendar/event' (reminder when in the reminders calendar, + ' weekly'
        etc. when it repeats, + ' @place' with a place), a deleted one as
        'POST /calendar/delete', never reaching HA.
        """
        cal = message.get("entity_id", "")
        if message.get("type") == "calendar/event/delete":
            self.calls.append("POST /calendar/delete")
            self.events = [e for e in self.events if e["uid"] != message.get("uid")]
            return {"success": True}
        event = message.get("event") or {}
        tag = "reminder" if cal == config.ha_calendar_reminders else "event"
        freq = re.search(r"FREQ=(\w+)", event.get("rrule") or "")
        place = " @place" if event.get("location") else ""
        day = " all-day" if "T" not in str(event.get("dtstart")) else ""
        self.calls.append(f"POST /calendar/{tag}{f' {freq[1].lower()}' if freq else ''}{day}{place}")
        self.events.append(
            {
                "calendar": cal,
                "uid": f"uid{len(self.events)}-{len(self.calls)}",
                "summary": event.get("summary"),
                "start": {"dateTime": event.get("dtstart")},
                "location": event.get("location"),
            }
        )
        return {"success": True}

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        path = request.url.path
        query = parse_qs(request.url.query.decode())
        if host == "accounts.spotify.com":
            return httpx2.Response(200, json={"access_token": "fake", "expires_in": 3600})
        if host == self.ha_host:
            return self._ha(request, path)
        if host.split(".")[0] == "felt-voice":  # felt-voice:8421 in the container
            # her proactive lines (a held appointment going in) pre-render
            # their voice in the background - not a write, and it lands in
            # whichever case runs next
            return httpx2.Response(503)
        if host != "api.spotify.com":
            self.calls.append(f"{request.method} {host}{path}")
            return httpx2.Response(503)
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
            items = [self._track(f"{m[1]} song {i}", m[1]) for i in range(3)] if m else [self._track(q, q)]
            return httpx2.Response(200, json={"tracks": {"items": items}})
        if path == "/me/player/queue":
            if request.method == "POST":
                self.queue.append(query.get("uri", [""])[0])
                return httpx2.Response(204)
            return httpx2.Response(200, json={"queue": [{"uri": u} for u in self.queue]})
        if path in ("/me/player", "/me/player/currently-playing"):
            item = self.now
            return httpx2.Response(
                200,
                json={
                    "is_playing": self.playing,
                    "progress_ms": 1000,
                    "item": item,
                    "device": {"volume_percent": 50},
                },
            )
        if path == "/me/library/contains":
            return httpx2.Response(200, json=[False])
        if request.method == "GET":
            return httpx2.Response(200, json={"items": []})
        return httpx2.Response(204)


def _install(recorder: Recorder) -> None:
    real = httpx2.AsyncHTTPTransport.handle_async_request

    async def handle(self, request):
        if request.url.host in LLM_HOSTS:
            return await real(self, request)
        response = recorder.respond(request)
        await request.aread()
        return response

    httpx2.AsyncHTTPTransport.handle_async_request = handle  # type: ignore[method-assign]


class _SkillLog(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        msg = record.getMessage()
        if "skill call" in msg or "fast path" in msg or "claimed" in msg or msg.startswith("router"):
            self.lines.append(msg)


def _writes(calls: list[str]) -> list[str]:
    return [c for c in calls if not c.startswith("GET ")]


def _verdict(case: Case, writes: list[str], route: list[str]) -> bool:
    if case.skills and not any(f"skill call: {k}" in r for r in route for k in case.skills):
        return False
    stripped = [w.split("?")[0] for w in writes]
    if case.writes is not None:
        return set(stripped) == set(case.writes) and (not case.writes or len(stripped) >= len(case.writes))
    return not any(w.startswith(n) for w in stripped for n in case.never)


async def _streamed(message: str, config, session_id: str, timing: dict) -> ChatResult:
    """chat() through chat_streamed(): the chunks must add up to the reply
    (else it's a CRASH line); `timing` gets the seconds to the first chunk
    and the chunk count.
    """
    start = time.monotonic()
    chunks: list[str] = []
    result = None
    async for item in chat_streamed(message, config, session_id=session_id):
        if isinstance(item, ChatResult):
            result = item
            continue
        if not isinstance(item, str):
            continue  # ReplyMeta: the tag / ui_action ahead of chunk 0
        if not chunks:
            timing.setdefault("first_s", []).append(round(time.monotonic() - start, 2))
        chunks.append(item)
    assert result is not None
    timing.setdefault("chunks", []).append(len(chunks))
    if " ".join(chunks).split() != result.reply.split():
        raise AssertionError(f"chunks {chunks!r} != reply {result.reply!r}")
    return result


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--only")
    ap.add_argument("--note", help="only cases with this note, e.g. look / dj / llm")
    ap.add_argument("--stream", action="store_true", help="through chat_streamed() (WP-65)")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="felt-e2e-")
    config = load_config().model_copy(update={"db_path": f"{tmp}/felt.db"})
    # the day's first chat fires the morning brief (and the diary + its
    # greeting) in the background; its TTS + phone notify landed in whatever
    # case was running and failed it (2026-10-23)
    for name in ("brief.morning", "ambient.morning_greeting", "memory.diary"):
        features.set_enabled(config.db_path, name, False)
    # the semantic router in shadow mode (cm-6b0): the image's model, or a
    # one-off copy for a run before it's deployed
    if not os.path.isdir(config.router_model_dir):
        from fastembed import TextEmbedding

        config = config.model_copy(update={"router_model_dir": "/tmp/e2e-router-model"})
        await asyncio.to_thread(TextEmbedding, config.router_model, cache_dir=config.router_model_dir)
    await router.warm(config)
    recorder = Recorder()
    recorder.ha_host = httpx2.URL(config.ha_url or "http://-").host
    recorder.config = config
    _install(recorder)
    ha_calendar._ws_call = recorder.ws_call  # type: ignore[method-assign]
    log = _SkillLog()
    logging.getLogger("felt_core").addHandler(log)
    logging.getLogger("felt_core").setLevel(logging.INFO)

    cases = [
        c
        for c in CASES
        if (not args.only or args.only.lower() in c.say.lower()) and (not args.note or c.note == args.note)
    ]
    failed = 0
    for run in range(args.repeat):
        for i, case in enumerate(cases):
            recorder.calls.clear()
            recorder.events.clear()
            log.lines.clear()
            timing: dict = {}

            async def say(message: str, session_id: str) -> ChatResult:
                if args.stream:
                    return await _streamed(message, config, session_id, timing)  # ruff: ignore[function-uses-loop-variable]
                return await chat(message, config, session_id=session_id)

            try:
                # a conversation per case: earlier "couldn't reach" replies in
                # the history taught the model not to try (DJ state isn't
                # per-session, so the DJ cases still carry over)
                result = await say(case.say, f"e2e-{run}-{i}")
                reply = result.reply
                await router.drain()
                if case.then:
                    result = await say(case.then, f"e2e-{run}-{i}")
                    reply = f"{reply} || {result.reply}"
                    await router.drain()
                # no (further) answer: what she held goes in now, not in 30 s
                await appointment_hold.add_unanswered(config)
                # the next case is a new appointment, not this one again (cm-2hu guard)
                ha_calendar.forget_created(config)
            except Exception as e:
                reply = f"CRASH {type(e).__name__}: {e}"
            writes = _writes(recorder.calls)
            ok = _verdict(case, writes, log.lines) and not reply.startswith("CRASH")
            if case.place is not None:
                # one appointment at the end, with or without its place
                ok = ok and [bool(e["location"]) for e in recorder.events] == [case.place]
            if case.summary_has:
                ok = ok and all(w in (e["summary"] or "").lower() for e in recorder.events for w in case.summary_has)
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
                        "events": [e["summary"] for e in recorder.events],
                        **timing,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    print(f"{len(cases) * args.repeat - failed}/{len(cases) * args.repeat} ok", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
