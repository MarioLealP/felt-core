"""The `facts` table."""

from sqlmodel import col, select

from felt_core.db.session import session
from felt_core.db.tables import Fact


async def save_fact(db_path: str, content: str) -> None:
    """Save one fact about Mario."""
    async with session(db_path) as s:
        s.add(Fact(content=content))
        await s.commit()


async def get_all_facts(db_path: str) -> list[str]:
    """Every saved fact, oldest first."""
    async with session(db_path) as s:
        return list((await s.exec(select(Fact.content).order_by(col(Fact.id)))).all())
