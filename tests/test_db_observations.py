from felt_core.db.observations import (
    get_recent_observations,
    save_observation,
    seconds_since_last_spoken_reaction,
    set_observation_reply,
)


async def test_save_and_get_recent_observations_chronological(tmp_path):
    db_path = str(tmp_path / "felt.db")
    await save_observation(db_path, "default", "first")
    await save_observation(db_path, "default", "second")
    await save_observation(db_path, "default", "third")

    observations = await get_recent_observations(db_path, "default", limit=3)

    assert [o["description"] for o in observations] == ["first", "second", "third"]


async def test_get_recent_observations_respects_limit(tmp_path):
    db_path = str(tmp_path / "felt.db")
    for i in range(5):
        await save_observation(db_path, "default", f"obs{i}")

    observations = await get_recent_observations(db_path, "default", limit=2)

    assert [o["description"] for o in observations] == ["obs3", "obs4"]


async def test_get_recent_observations_scoped_per_session(tmp_path):
    db_path = str(tmp_path / "felt.db")
    await save_observation(db_path, "a", "for a")
    await save_observation(db_path, "b", "for b")

    observations = await get_recent_observations(db_path, "a", limit=3)

    assert [o["description"] for o in observations] == ["for a"]


async def test_spoken_reply_starts_none(tmp_path):
    db_path = str(tmp_path / "felt.db")
    await save_observation(db_path, "default", "something")

    observations = await get_recent_observations(db_path, "default", limit=1)

    assert observations[0]["spoken_reply"] is None


async def test_set_observation_reply_tracks_it(tmp_path):
    db_path = str(tmp_path / "felt.db")
    obs_id = await save_observation(db_path, "default", "something")
    await set_observation_reply(db_path, obs_id, "haha nice")

    observations = await get_recent_observations(db_path, "default", limit=1)

    assert observations[0]["spoken_reply"] == "haha nice"


async def test_seconds_since_last_spoken_reaction_none_when_never_spoken(tmp_path):
    db_path = str(tmp_path / "felt.db")
    await save_observation(db_path, "default", "something")  # spoken_reply stays NULL

    assert await seconds_since_last_spoken_reaction(db_path, "default") is None


async def test_seconds_since_last_spoken_reaction_is_recent_just_after_speaking(tmp_path):
    db_path = str(tmp_path / "felt.db")
    obs_id = await save_observation(db_path, "default", "something")
    await set_observation_reply(db_path, obs_id, "reacted")

    seconds = await seconds_since_last_spoken_reaction(db_path, "default")

    assert seconds is not None
    assert 0 <= seconds < 5


async def test_seconds_since_last_spoken_reaction_scoped_per_session(tmp_path):
    db_path = str(tmp_path / "felt.db")
    obs_id = await save_observation(db_path, "a", "something")
    await set_observation_reply(db_path, obs_id, "reacted")

    assert await seconds_since_last_spoken_reaction(db_path, "b") is None
