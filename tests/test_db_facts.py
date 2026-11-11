"""The `facts` table through the async ORM session."""

from felt_core.db.facts import get_all_facts, save_fact


async def test_facts_come_back_oldest_first(tmp_path):
    db = str(tmp_path / "felt.db")
    assert await get_all_facts(db) == []

    await save_fact(db, "likes tea")
    await save_fact(db, "hates mornings")

    assert await get_all_facts(db) == ["likes tea", "hates mornings"]
