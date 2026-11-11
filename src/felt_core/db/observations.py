"""The `screen_observations` table: what she saw on the screen and what she said about it."""

from sqlalchemy import func, update
from sqlmodel import col, select

from felt_core.db.session import require_id, session
from felt_core.db.tables import ScreenObservation as Obs


async def save_observation(db_path: str, session_id: str, description: str) -> int:
    """Save one screen observation and return its id."""
    row = Obs(session_id=session_id, description=description)
    async with session(db_path) as s:
        s.add(row)
        await s.commit()
    return require_id(row)


async def set_observation_reply(db_path: str, obs_id: int, reply: str) -> None:
    """Store the spoken reply on one observation."""
    async with session(db_path) as s:
        await s.exec(update(Obs).where(col(Obs.id) == obs_id).values(spoken_reply=reply))
        await s.commit()


async def get_recent_observations(db_path: str, session_id: str, limit: int = 3) -> list[dict]:
    """The most recent `limit` observations for the session, oldest first."""
    # newest `limit` first, then reversed: "most recent N, chronological order", like get_history
    query = select(Obs).where(Obs.session_id == session_id).order_by(col(Obs.id).desc()).limit(limit)
    async with session(db_path) as s:
        rows = list(await s.exec(query))
    return [
        {"id": r.id, "description": r.description, "spoken_reply": r.spoken_reply, "created_at": r.created_at}
        for r in reversed(rows)
    ]


async def seconds_since_last_spoken_reaction(db_path: str, session_id: str) -> float | None:
    """None if she's never actually spoken a screen reaction in this session.

    the caller (the cooldown check) treats that as "not on cooldown," not as "infinitely
    long ago." Age math done in SQL (julianday) - `created_at` is SQLite's own
    `datetime('now')` UTC string, and comparing it in Python would mean parsing that
    format back out for no benefit.
    """
    age = (func.julianday("now") - func.julianday(Obs.created_at)) * 86400.0
    query = (
        select(age)
        .where(Obs.session_id == session_id, col(Obs.spoken_reply).is_not(None))
        .order_by(col(Obs.id).desc())
        .limit(1)
    )
    async with session(db_path) as s:
        return (await s.exec(query)).first()


async def get_current_screen_description(db_path: str, session_id: str, max_age_seconds: int) -> str | None:
    """The most recent observation for the session.

    if it's recent enough to still count as "what's currently on screen" (WP-02: normal
    chat gets a "Currently on Mario's screen: ..." system message when this is fresh, so
    "what do you think of this?" has something to refer to).
    """
    query = (
        select(Obs.description)
        .where(
            Obs.session_id == session_id,
            col(Obs.created_at) >= func.datetime("now", f"-{max_age_seconds} seconds"),
        )
        .order_by(col(Obs.id).desc())
        .limit(1)
    )
    async with session(db_path) as s:
        return (await s.exec(query)).first()


async def get_screen_observations_between(db_path: str, start_utc: str, end_utc: str) -> list[dict]:
    """List the screen observations recorded between `start_utc` and `end_utc`."""
    query = select(Obs).where(col(Obs.created_at) >= start_utc, col(Obs.created_at) < end_utc).order_by(col(Obs.id))
    async with session(db_path) as s:
        return [{"description": r.description, "spoken_reply": r.spoken_reply} for r in await s.exec(query)]
