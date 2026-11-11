"""Alembic's environment: felt.db's schema is SQLModel.metadata (db/tables.py); run through db/migrate.py."""

from alembic import context
from sqlalchemy import create_engine
from sqlmodel import SQLModel

import felt_core.db.tables  # ruff: ignore[unused-import]  # registers every table on SQLModel.metadata

url = context.config.get_main_option("sqlalchemy.url")
if url is None:
    raise RuntimeError("run migrations through felt_core.db.migrate, which sets sqlalchemy.url")
engine = create_engine(url)
with engine.connect() as connection:
    # render_as_batch: SQLite can't ALTER most things, so Alembic rebuilds the table instead
    context.configure(connection=connection, target_metadata=SQLModel.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
