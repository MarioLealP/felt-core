"""WP-26: the diary's material gatherer - whitelisted event lines,
dedupe/cap, event-data capping, and the privacy line (chat only as a
whole safe window, facts never touched at all)."""

import json

import felt_core.diary as diary
from felt_core.config import FeltConfig
from felt_core.db import _connect, save_event, save_fact, save_message


def _config(tmp_path):
    return FeltConfig(db_path=str(tmp_path / "felt.db"))


def test_event_line_whitelists_known_kinds():
    assert (
        diary._event_line("game_start", {"name": "endfield.exe"})
        == 'started playing "endfield.exe"'
    )
    assert diary._event_line("day_start", {}) is None
    assert diary._event_line("unknown_kind", {"secret": "do not send"}) is None


def test_event_line_game_stop_formats_duration():
    line = diary._event_line("game_stop", {"name": "poe2.exe", "duration_s": 7200})
    assert line == 'stopped playing "poe2.exe" (about 120 min)'


def test_compact_material_dedupes_repeated_lines_and_keeps_order():
    raw = diary.RawMaterial(
        seen=[{"description": "a game", "spoken_reply": None}] * 3,
        events=[],
        chat_windows=[],
    )
    text = diary.compact_material(raw)
    assert text.count("a game") == 1


def test_compact_material_caps_total_length():
    raw = diary.RawMaterial(
        seen=[{"description": f"unique observation {i}", "spoken_reply": None} for i in range(500)],
        events=[],
        chat_windows=[],
    )
    text = diary.compact_material(raw)
    assert len(text) <= diary._MAX_MATERIAL_CHARS


def test_cap_event_data_keeps_small_payloads_intact():
    assert json.loads(diary.cap_event_data({"name": "x"})) == {"name": "x"}


def test_cap_event_data_truncates_oversized_payloads():
    encoded = diary.cap_event_data({"blob": "x" * 5000})
    assert len(encoded) < 5000
    assert json.loads(encoded)["_truncated"] is True


async def test_gather_material_excludes_unsafe_chat_and_never_touches_facts(tmp_path, monkeypatch):
    config = _config(tmp_path)
    save_fact(config.db_path, "Mario's home address is a private secret")
    save_message(config.db_path, "s1", "user", "safe banter about bread")
    save_message(config.db_path, "s2", "user", "my medical history is private stuff")
    conn = _connect(config.db_path)
    with conn:
        conn.execute("UPDATE messages SET created_at = '2026-09-20 12:00:00'")
    conn.close()
    save_event(
        config.db_path,
        "music_track",
        json.dumps({"title": "Song", "artist": "Artist"}),
        "2026-09-20 12:00:00",
    )

    async def fake_is_safe(text, cfg):
        return "medical" not in text

    monkeypatch.setattr(diary, "is_safe_for_cloud", fake_is_safe)

    material = await diary.gather_material(config, "2026-09-20")

    assert "bread" in material
    assert "medical" not in material
    assert "secret" not in material
    assert 'played "Song" by Artist' in material
