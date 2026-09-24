"""POST /v1/react/event (events.py): deterministic cooldown/dedupe/min-gap
decisions, the whitelist-only prompt, and the guarantee that an event is
never stored as a screen observation."""

from urllib.parse import unquote

from fastapi.testclient import TestClient

import felt_core.api as api
import felt_core.events as events
from felt_core.config import FeltConfig
from felt_core.db import (
    get_history,
    get_recent_observations,
    save_observation,
    seconds_since_last_spoken_reaction,
    set_observation_reply,
)

_TRACK = {"title": "Song A", "artist": "Artist A", "album": "Album A"}


def _config(tmp_path, **kwargs):
    kwargs.setdefault("gemini_api_key", "fake-key")
    return FeltConfig(db_path=str(tmp_path / "felt.db"), **kwargs)


def _fake_cloud(monkeypatch, line="ooh, good pick"):
    calls = []

    async def fake_cloud_generate(purpose, system, messages, cfg, **kwargs):
        calls.append({"purpose": purpose, "system": system, "messages": messages})
        return line

    monkeypatch.setattr(events, "cloud_generate", fake_cloud_generate)
    return calls


# -- decide(): pure ------------------------------------------------------


def _decide(event, details=None, **ages):
    ages = {"since_any_remark": None, "since_event": None, "since_same": None, **ages}
    return events.decide(event, details or {}, events.DEFAULT_POLICIES, min_gap_s=30, **ages)


def test_unknown_event_is_no_reaction():
    assert _decide("tell_a_joke") == "unknown_event"


def test_first_event_speaks():
    assert _decide("music_new_track", _TRACK) is None


def test_event_cooldown():
    assert _decide("music_new_track", _TRACK, since_event=60) == "cooldown"
    assert _decide("music_new_track", _TRACK, since_event=6 * 60) is None


def test_global_min_gap_covers_every_remark():
    assert _decide("gpu_hot", since_any_remark=10) == "min_gap"


def test_same_track_is_deduped_after_the_cooldown_passes():
    assert _decide("music_new_track", _TRACK, since_event=10 * 60, since_same=10 * 60) == "duplicate"


def test_afk_end_only_speaks_after_a_long_absence():
    assert _decide("afk_end", {"duration_s": 12 * 60}) == "too_short"
    assert _decide("afk_end", {}) == "too_short"
    assert _decide("afk_end", {"duration_s": 45 * 60}) is None


def test_game_stop_needs_a_real_session():
    assert _decide("game_stop", {"name": "endfield.exe", "duration_s": 60}) == "too_short"
    assert _decide("game_stop", {"name": "endfield.exe", "duration_s": 3600}) is None


def test_policy_override_from_config(tmp_path):
    config = _config(tmp_path, event_policies='{"music_new_track": {"cooldown_s": 5}}')
    assert events.policies(config)["music_new_track"].cooldown_s == 5
    assert events.policies(config)["music_new_track"].dedupe_s == events.DEFAULT_POLICIES["music_new_track"].dedupe_s


# -- prompts: whitelisted fields only -------------------------------------


def test_prompt_uses_only_whitelisted_fields():
    prompt = events.build_prompt("music_new_track", {**_TRACK, "secret": "do not send"}, lyric="la la la")
    assert '"Song A" by Artist A' in prompt
    assert "la la la" in prompt
    assert "do not send" not in prompt


def test_game_prompt_strips_exe_and_formats_duration():
    prompt = events.build_prompt("game_stop", {"name": "endfield.exe", "duration_s": 7200})
    assert "endfield" in prompt and ".exe" not in prompt
    assert "about 2 hours" in prompt


def test_music_without_title_is_too_thin():
    assert events.build_prompt("music_new_track", {"artist": "x"}) is None


# -- react_to_event --------------------------------------------------------


async def test_music_event_speaks_then_cools_down_with_zero_model_calls(tmp_path, monkeypatch):
    config = _config(tmp_path)
    calls = _fake_cloud(monkeypatch)

    first = await events.react_to_event("music_new_track", _TRACK, config, session_id="s")
    second = await events.react_to_event(
        "music_new_track", {"title": "Song B", "artist": "Artist B"}, config, session_id="s"
    )

    assert first.line == "ooh, good pick" and first.emotion == "happy"
    assert second.line == "" and second.skipped == "min_gap"
    assert len(calls) == 1
    assert calls[0]["purpose"] == "event_reaction"
    assert all(m["role"] == "user" for m in calls[0]["messages"])


async def test_event_is_never_a_screen_observation(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _fake_cloud(monkeypatch)

    result = await events.react_to_event("music_new_track", _TRACK, config, session_id="s")

    assert result.line
    assert get_recent_observations(config.db_path, "s", limit=10) == []
    assert seconds_since_last_spoken_reaction(config.db_path, "s") is None  # screen cooldown untouched
    # Her line is in chat history, but not as "real conversation".
    assert [m["content"] for m in get_history(config.db_path, "s")] == [result.line]
    assert get_history(config.db_path, "s", exclude_remarks=True) == []


async def test_a_recent_screen_remark_blocks_an_event_line(tmp_path, monkeypatch):
    config = _config(tmp_path)
    calls = _fake_cloud(monkeypatch)
    obs = save_observation(config.db_path, "s", "a game")
    set_observation_reply(config.db_path, obs, "nice")

    result = await events.react_to_event("gpu_hot", {"temp_c": 85}, config, session_id="s")

    assert result.skipped == "min_gap"
    assert calls == []


async def test_unknown_event_makes_no_model_call(tmp_path, monkeypatch):
    config = _config(tmp_path)
    calls = _fake_cloud(monkeypatch)

    result = await events.react_to_event("mash", {}, config)

    assert result.skipped == "unknown_event"
    assert calls == []


async def test_game_events_go_through_the_privacy_check(tmp_path, monkeypatch):
    import felt_core.brain as brain
    import felt_core.privacy as privacy

    config = _config(tmp_path)
    calls = _fake_cloud(monkeypatch)
    checked = []

    async def fake_is_safe(text, cfg):
        checked.append(text)
        return False

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        return "game time, huh?"

    monkeypatch.setattr(privacy, "is_safe_for_cloud", fake_is_safe)
    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    result = await events.react_to_event("game_start", {"name": "endfield.exe"}, config)

    assert result.line == "game time, huh?"
    assert checked and "endfield" in checked[0]
    assert calls == []  # PRIVATE -> never reached the cloud


async def test_cloud_failure_falls_back_to_local(tmp_path, monkeypatch):
    import felt_core.brain as brain
    from felt_core.cloud import CloudUnavailable

    config = _config(tmp_path)

    async def failing_cloud(*args, **kwargs):
        raise CloudUnavailable("exhausted")

    async def fake_ollama_chat(messages, model, cfg, **kwargs):
        return "welcome back!"

    monkeypatch.setattr(events, "cloud_generate", failing_cloud)
    monkeypatch.setattr(brain, "_ollama_chat", fake_ollama_chat)

    result = await events.react_to_event("afk_end", {"duration_s": 3600}, config)

    assert result.line == "welcome back!"


# -- the endpoint: same contract as /v1/react/screen -------------------------


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_VOICE_URL", "http://voice.invalid")
    monkeypatch.setenv("FELT_GEMINI_API_KEY", "fake-key")
    monkeypatch.delenv("FELT_SPOTIFY_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr(api.tts_cache, "get", lambda text: b"RIFF-fake-wav")
    return TestClient(api.app)  # no lifespan - no background loops


def test_endpoint_speaks_with_the_screen_reaction_contract(tmp_path, monkeypatch):
    _fake_cloud(monkeypatch, line="this one again?")
    client = _client(tmp_path, monkeypatch)

    response = client.post("/v1/react/event", json={"event": "music_new_track", "details": _TRACK})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == b"RIFF-fake-wav"
    assert unquote(response.headers["X-Reply-Text"]) == "this one again?"
    assert response.headers["X-Emotion"] == "happy"


def test_endpoint_no_reaction_shape(tmp_path, monkeypatch):
    _fake_cloud(monkeypatch)
    client = _client(tmp_path, monkeypatch)

    response = client.post("/v1/react/event", json={"event": "not_a_real_event", "details": {}})

    assert response.status_code == 200
    assert response.content == b""
    assert "X-Reply-Text" not in response.headers
    assert response.headers["X-Reaction-Skipped"] == "unknown_event"
