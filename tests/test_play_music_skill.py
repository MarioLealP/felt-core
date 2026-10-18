from conftest import load_plugin_module

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext


def _load_skill():
    return load_plugin_module("play_music/skill.py")


async def test_reports_unconfigured_when_no_refresh_token():
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token=None)
    ctx = ExecutionContext(config=config, original_message="play Bohemian Rhapsody")

    result = await skill.handle({"query": "Bohemian Rhapsody"}, ctx)

    assert "isn't connected" in result.text


async def test_asks_what_to_play_when_query_is_blank():
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token="dummy-token")
    ctx = ExecutionContext(config=config, original_message="")

    result = await skill.handle({"query": "   "}, ctx)

    assert "what should i play" in result.text.lower()


async def _run(monkeypatch, params):
    skill = _load_skill()
    calls = []
    track = {"uri": "u:x", "name": "Birdbrain", "artists": [{"name": "Jamie Paige"}]}

    async def search(client, config, query):
        return track

    async def queued(client, config, uri):
        calls.append(("queue", uri))

    async def started(client, config, uri, name=None):
        calls.append(("start", uri))

    monkeypatch.setattr(skill, "search_track", search)
    monkeypatch.setattr(skill, "add_to_queue", queued)
    monkeypatch.setattr(skill, "start_track", started)
    config = FeltConfig(spotify_refresh_token="dummy-token")
    result = await skill.handle(params, ExecutionContext(config=config, original_message=""))
    return calls, result.text


async def test_plays_now_by_default(monkeypatch):
    calls, text = await _run(monkeypatch, {"query": "birdbrain"})
    assert calls == [("start", "u:x")] and text.startswith("Playing")


async def test_queue_only_queues(monkeypatch):
    # Mario 2026-10-18: "make it different things, add to queue or play"
    calls, text = await _run(monkeypatch, {"query": "birdbrain", "when": "queue"})
    assert calls == [("queue", "u:x")] and text.startswith("Queued")
