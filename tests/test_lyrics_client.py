"""Pure LRC parsing/chorus-detection/current-line logic, plus the disk
cache behaviour of get_lyrics with a monkeypatched httpx layer (no real
network - see test_lyrics_client_integration.py for the real LRCLIB call,
recorded via VCR)."""

import json

import httpx

from felt_core.lyrics_client import current_line, find_chorus, get_lyrics, parse_lrc

_SAMPLE_LRC = (
    "[ar:Test Artist]\n"
    "[ti:Test Song]\n"
    "[00:00.00]\n"
    "[00:05.50]First verse line\n"
    "[00:10.00]Another verse line\n"
    "[00:15.00]This is the chorus\n"
    "[00:20.00]Second verse begins\n"
    "[00:25.00]More second verse\n"
    "[00:30.00]This is the chorus\n"
    "[00:35.00]Bridge line\n"
    "[00:40.00]This is the chorus\n"
)


# -- parse_lrc ----------------------------------------------------------


def test_parse_lrc_extracts_timestamps_and_text():
    lines = parse_lrc("[00:05.50]Hello there\n[01:02.25]Second line")
    assert lines == [
        {"time_ms": 5500, "text": "Hello there"},
        {"time_ms": 62250, "text": "Second line"},
    ]


def test_parse_lrc_drops_metadata_lines_without_a_timestamp():
    lines = parse_lrc("[ar:Someone]\n[00:01.00]Real lyric")
    assert lines == [{"time_ms": 1000, "text": "Real lyric"}]


def test_parse_lrc_keeps_empty_lines_for_instrumental_gaps():
    lines = parse_lrc("[00:00.00]\n[00:10.00]First words")
    assert lines[0] == {"time_ms": 0, "text": ""}


def test_parse_lrc_sorts_by_time():
    lines = parse_lrc("[00:10.00]Second\n[00:01.00]First")
    assert [line["text"] for line in lines] == ["First", "Second"]


# -- find_chorus ----------------------------------------------------------


def test_find_chorus_picks_the_most_repeated_line():
    chorus = find_chorus(parse_lrc(_SAMPLE_LRC))
    assert chorus["text"] == "This is the chorus"
    assert chorus["start_ms"] == 15000


def test_find_chorus_end_ms_is_the_next_lines_start():
    chorus = find_chorus(parse_lrc(_SAMPLE_LRC))
    assert chorus["end_ms"] == 20000


def test_find_chorus_none_when_nothing_repeats_enough():
    lines = parse_lrc("[00:01.00]One\n[00:02.00]Two\n[00:03.00]One")
    assert find_chorus(lines) is None


def test_find_chorus_none_on_empty_lyrics():
    assert find_chorus([]) is None


# -- current_line ----------------------------------------------------------


def test_current_line_returns_the_active_line():
    lines = parse_lrc(_SAMPLE_LRC)
    assert current_line(lines, 16000) == "This is the chorus"


def test_current_line_none_before_the_first_line():
    lines = parse_lrc(_SAMPLE_LRC)
    assert current_line(lines, 0) is None  # first line (t=0) is an empty gap


def test_current_line_none_on_an_instrumental_gap():
    lines = parse_lrc("[00:00.00]\n[00:10.00]Words start here")
    assert current_line(lines, 5000) is None


def test_current_line_none_with_no_lines():
    assert current_line([], 5000) is None


# -- get_lyrics (disk cache, monkeypatched HTTP) ---------------------------


class _FakeResponse:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


async def test_get_lyrics_caches_a_hit_to_disk(tmp_path, monkeypatch):
    async def fake_get(self, url, params, headers):
        return _FakeResponse(200, {"syncedLyrics": "[00:01.00]Hi", "plainLyrics": "Hi"})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    async with httpx.AsyncClient() as client:
        result = await get_lyrics(client, tmp_path, "track1", "Song", "Artist", "Album", 200)

    assert result["synced"] == [{"time_ms": 1000, "text": "Hi"}]
    cached = json.loads((tmp_path / "track1.json").read_text())
    assert cached["result"]["plain"] == "Hi"


async def test_get_lyrics_caches_a_miss_to_disk(tmp_path, monkeypatch):
    calls = []

    async def fake_get(self, url, params, headers):
        calls.append(1)
        return _FakeResponse(404, {})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    async with httpx.AsyncClient() as client:
        first = await get_lyrics(client, tmp_path, "track2", "Song", "Artist", "Album", 200)
        second = await get_lyrics(client, tmp_path, "track2", "Song", "Artist", "Album", 200)

    assert first is None
    assert second is None
    assert len(calls) == 1  # the second call hit the cached-miss file, not the network


async def test_get_lyrics_does_not_cache_a_network_failure(tmp_path, monkeypatch):
    async def fake_get(self, url, params, headers):
        raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    async with httpx.AsyncClient() as client:
        result = await get_lyrics(client, tmp_path, "track3", "Song", "Artist", "Album", 200)

    assert result is None
    assert not (tmp_path / "track3.json").exists()


async def test_get_lyrics_falls_back_to_plain_only_when_no_synced_lyrics(tmp_path, monkeypatch):
    async def fake_get(self, url, params, headers):
        return _FakeResponse(200, {"syncedLyrics": None, "plainLyrics": "Just plain text"})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    async with httpx.AsyncClient() as client:
        result = await get_lyrics(client, tmp_path, "track4", "Song", "Artist", "Album", 200)

    assert result == {"synced": [], "plain": "Just plain text"}
