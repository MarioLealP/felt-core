"""Personality plan P4 self-memory (merged into WP-26): add/dedupe/veto,
the <=30 active cap, the prompt ranking (pinned > keyword > recency,
cooldown), callback decay, and memory_block's hard budget."""

import json
from datetime import UTC, datetime, timedelta

import felt_core.self_memory as self_memory
from felt_core.config import FeltConfig
from felt_core.db import (
    get_self_memories,
    insert_diary,
    insert_self_memory,
    update_self_memory,
)


def _config(tmp_path):
    return FeltConfig(db_path=str(tmp_path / "felt.db"))


# -- is_duplicate / add_self_memory -----------------------------------------


def test_is_duplicate_catches_high_word_overlap():
    assert self_memory.is_duplicate("bread is the best food", ["bread is the best food ever"])
    assert not self_memory.is_duplicate("bread is great", ["poe2 crafting is a job"])


def test_add_self_memory_skips_vetoed_text(tmp_path):
    config = _config(tmp_path)
    mid = insert_self_memory(
        config.db_path, "opinion", "factorio is a condition", "[]", "2026-09-01"
    )
    update_self_memory(config.db_path, mid, status="vetoed")

    result = self_memory.add_self_memory(
        config, "opinion", "factorio is a condition", [], "2026-09-02"
    )

    assert result is None
    assert get_self_memories(config.db_path, status="active") == []


def test_add_self_memory_skips_near_duplicate_actives(tmp_path):
    config = _config(tmp_path)
    self_memory.add_self_memory(
        config, "opinion", "bread is the best food", ["bread"], "2026-09-01"
    )

    result = self_memory.add_self_memory(
        config, "opinion", "bread is truly the best food", ["bread"], "2026-09-02"
    )

    assert result is None
    assert len(get_self_memories(config.db_path, status="active")) == 1


def test_add_self_memory_enforces_the_30_active_cap(tmp_path):
    config = _config(tmp_path)
    for i in range(30):
        self_memory.add_self_memory(config, "opinion", f"w{i}a w{i}b w{i}c w{i}d", [], "2026-09-01")

    self_memory.add_self_memory(
        config, "opinion", "one more totally new opinion here", [], "2026-09-02"
    )

    active = get_self_memories(config.db_path, status="active")
    retired = get_self_memories(config.db_path, status="retired")
    assert len(active) == self_memory.MAX_ACTIVE
    assert len(retired) == 1
    assert retired[0]["text"] == "w0a w0b w0c w0d"


def test_add_self_memory_never_retires_a_pinned_one(tmp_path):
    config = _config(tmp_path)
    first_id = self_memory.add_self_memory(
        config, "opinion", "opinion number 0 about something unique", [], "2026-09-01"
    )
    assert first_id is not None
    update_self_memory(config.db_path, first_id, pinned=True)
    for i in range(1, 30):
        self_memory.add_self_memory(config, "opinion", f"w{i}a w{i}b w{i}c w{i}d", [], "2026-09-01")

    self_memory.add_self_memory(
        config, "opinion", "one more totally new opinion here", [], "2026-09-02"
    )

    retired_texts = [m["text"] for m in get_self_memories(config.db_path, status="retired")]
    assert "opinion number 0 about something unique" not in retired_texts


# -- rank_self_memories: pure -------------------------------------------------


def test_rank_self_memories_pins_first_then_keyword_then_recency():
    now = datetime.now(UTC)
    memories = [
        {
            "id": 1,
            "text": "a",
            "keywords_json": "[]",
            "pinned": False,
            "last_used": None,
            "created_at": "2026-01-01 00:00:00",
        },
        {
            "id": 2,
            "text": "b",
            "keywords_json": json.dumps(["poe2"]),
            "pinned": False,
            "last_used": None,
            "created_at": "2026-01-02 00:00:00",
        },
        {
            "id": 3,
            "text": "c",
            "keywords_json": "[]",
            "pinned": True,
            "last_used": None,
            "created_at": "2026-01-01 00:00:00",
        },
    ]

    ranked = self_memory.rank_self_memories(memories, "playing poe2 tonight", now)

    assert [m["id"] for m in ranked] == [3, 2, 1]


def test_rank_self_memories_applies_cooldown_unless_pool_would_empty():
    now = datetime.now(UTC)
    recent = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    old = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    memories = [
        {
            "id": 1,
            "text": "a",
            "keywords_json": "[]",
            "pinned": False,
            "last_used": recent,
            "created_at": "x",
        },
        {
            "id": 2,
            "text": "b",
            "keywords_json": "[]",
            "pinned": False,
            "last_used": old,
            "created_at": "x",
        },
    ]

    ranked = self_memory.rank_self_memories(memories, None, now)
    assert ranked[0]["id"] == 2  # id 1 is on cooldown, excluded first

    ranked_only_cooled = self_memory.rank_self_memories([memories[0]], None, now)
    assert [m["id"] for m in ranked_only_cooled] == [
        1
    ]  # cooldown would empty the pool - used anyway


def test_rank_self_memories_caps_to_max_in_prompt():
    now = datetime.now(UTC)
    memories = [
        {
            "id": i,
            "text": f"m{i}",
            "keywords_json": "[]",
            "pinned": False,
            "last_used": None,
            "created_at": f"2026-01-{i:02d} 00:00:00",
        }
        for i in range(1, 12)
    ]

    ranked = self_memory.rank_self_memories(memories, None, now)

    assert len(ranked) == self_memory.MAX_IN_PROMPT


# -- callback decay -----------------------------------------------------------


def test_pick_callbacks_decays_after_two_uses():
    callbacks = [{"text": "the boss fight", "uses": 0}]

    picked, updated = self_memory._pick_callbacks(callbacks)
    assert picked == ["the boss fight"] and updated[0]["uses"] == 1

    picked2, updated2 = self_memory._pick_callbacks(updated)
    assert picked2 == ["the boss fight"] and updated2[0]["uses"] == 2

    picked3, _ = self_memory._pick_callbacks(updated2)
    assert picked3 == []  # retired after 2 uses


def test_pick_callbacks_prefers_least_used():
    callbacks = [{"text": "used once", "uses": 1}, {"text": "fresh", "uses": 0}]
    picked, _ = self_memory._pick_callbacks(callbacks, max_n=1)
    assert picked == ["fresh"]


# -- memory_block: the prompt budget -----------------------------------------


def test_memory_block_caps_at_eight_bullets_and_the_char_budget(tmp_path):
    config = _config(tmp_path)
    for i in range(12):
        insert_self_memory(
            config.db_path,
            "opinion",
            f"opinion {i} about something distinct enough",
            "[]",
            "2026-09-01",
        )

    block = self_memory.memory_block(config)

    assert block.count("\n- ") <= self_memory.MAX_IN_PROMPT
    assert len(block) <= self_memory.MAX_BLOCK_CHARS


def test_memory_block_includes_yesterdays_diary_snippet(tmp_path):
    config = _config(tmp_path)
    now = datetime.now(self_memory.LISBON)
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    insert_diary(
        config.db_path,
        yesterday,
        "a fine day",
        json.dumps([{"text": "boss fight", "uses": 0}]),
        "cozy",
    )

    block = self_memory.memory_block(config, now=now)

    assert "cozy" in block
    assert "boss fight" in block


def test_memory_block_touches_uses_on_selected_memories(tmp_path):
    config = _config(tmp_path)
    insert_self_memory(
        config.db_path, "opinion", "a fresh opinion nobody used yet", "[]", "2026-09-01"
    )

    self_memory.memory_block(config)

    row = get_self_memories(config.db_path, status="active")[0]
    assert row["uses"] == 1
    assert row["last_used"] is not None


def test_memory_block_empty_on_an_empty_db(tmp_path):
    config = _config(tmp_path)
    assert self_memory.memory_block(config) == ""
