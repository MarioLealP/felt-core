"""WP-25 db helpers: insert/claim/requeue/prune for the proactive queue."""

from felt_core.db.proactive import (
    claim_next_proactive,
    get_proactive_item,
    insert_proactive,
    prune_expired_proactive,
    requeue_proactive,
)


async def test_claim_returns_the_oldest_undelivered_item(tmp_path):
    db_path = str(tmp_path / "felt.db")
    first = await insert_proactive(db_path, "diary", "first", "{}", "9999-01-01 00:00:00")
    await insert_proactive(db_path, "diary", "second", "{}", "9999-01-01 00:00:00")

    claimed = await claim_next_proactive(db_path)

    assert claimed is not None
    assert claimed["id"] == first
    assert claimed["text"] == "first"


async def test_claim_marks_delivered_so_it_is_not_handed_out_twice(tmp_path):
    db_path = str(tmp_path / "felt.db")
    await insert_proactive(db_path, "diary", "only one", "{}", "9999-01-01 00:00:00")

    first = await claim_next_proactive(db_path)
    second = await claim_next_proactive(db_path)

    assert first is not None
    assert second is None


async def test_expired_item_is_never_claimed(tmp_path):
    db_path = str(tmp_path / "felt.db")
    await insert_proactive(db_path, "urgent", "too late", "{}", "2000-01-01 00:00:00")

    assert await claim_next_proactive(db_path) is None


async def test_null_expiry_never_expires(tmp_path):
    db_path = str(tmp_path / "felt.db")
    await insert_proactive(db_path, "diary", "no expiry", "{}", None)

    claimed = await claim_next_proactive(db_path)

    assert claimed is not None
    assert claimed["text"] == "no expiry"


async def test_requeue_makes_a_delivered_item_claimable_again(tmp_path):
    db_path = str(tmp_path / "felt.db")
    item_id = await insert_proactive(db_path, "diary", "hi", "{}", "9999-01-01 00:00:00")
    await claim_next_proactive(db_path)

    requeued = await requeue_proactive(db_path, item_id)
    claimed_again = await claim_next_proactive(db_path)

    assert requeued is True
    assert claimed_again is not None
    assert claimed_again["id"] == item_id


async def test_requeue_refuses_an_expired_item(tmp_path):
    db_path = str(tmp_path / "felt.db")
    item_id = await insert_proactive(db_path, "diary", "stale", "{}", "2000-01-01 00:00:00")

    requeued = await requeue_proactive(db_path, item_id)

    assert requeued is False
    assert await claim_next_proactive(db_path) is None


async def test_requeue_unknown_id_is_false(tmp_path):
    db_path = str(tmp_path / "felt.db")
    assert await requeue_proactive(db_path, 12345) is False


async def test_get_proactive_item_returns_full_row(tmp_path):
    db_path = str(tmp_path / "felt.db")
    item_id = await insert_proactive(db_path, "urgent", "dentist in 10", "{}", "9999-01-01 00:00:00")

    row = await get_proactive_item(db_path, item_id)

    assert row is not None
    assert row["kind"] == "urgent"
    assert row["text"] == "dentist in 10"


async def test_get_proactive_item_unknown_id_is_none(tmp_path):
    db_path = str(tmp_path / "felt.db")
    assert await get_proactive_item(db_path, 999) is None


async def test_prune_removes_only_expired_rows(tmp_path):
    db_path = str(tmp_path / "felt.db")
    keep = await insert_proactive(db_path, "diary", "keep me", "{}", "9999-01-01 00:00:00")
    await insert_proactive(db_path, "diary", "prune me", "{}", "2000-01-01 00:00:00")

    await prune_expired_proactive(db_path)

    assert await get_proactive_item(db_path, keep) is not None
    remaining_texts = [row["text"] for row in [await get_proactive_item(db_path, keep)] if row is not None]
    assert remaining_texts == ["keep me"]
