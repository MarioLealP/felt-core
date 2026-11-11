"""The `settings` table: small key/value state."""

from typing import overload

from sqlalchemy.dialects.sqlite import insert
from sqlmodel import col, select

from felt_core.db.session import session
from felt_core.db.tables import Setting


@overload
async def get_setting(db_path: str, key: str, default: str) -> str: ...
@overload
async def get_setting(db_path: str, key: str, default: None = None) -> str | None: ...
async def get_setting(db_path: str, key: str, default: str | None = None) -> str | None:
    """Return the stored value for a key, or the default when it is not set."""
    async with session(db_path) as s:
        row = await s.get(Setting, key)
    return row.value if row else default


async def set_setting(db_path: str, key: str, value: str) -> None:
    """Store a setting value, replacing any previous one."""
    upsert = insert(Setting).values(key=key, value=value)
    async with session(db_path) as s:
        await s.exec(upsert.on_conflict_do_update(index_elements=["key"], set_={"value": upsert.excluded.value}))
        await s.commit()


async def settings_with_prefix(db_path: str, prefix: str) -> dict[str, str]:
    """Every setting whose key starts with `prefix`, in one query."""
    async with session(db_path) as s:
        rows = await s.exec(select(Setting).where(col(Setting.key).startswith(prefix, autoescape=True)))
        return {row.key: row.value for row in rows}
