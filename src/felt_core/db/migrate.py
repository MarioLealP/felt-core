"""Bring felt.db to the newest schema with Alembic (db/migrations), once at startup.

A database from before Alembic (tables, no `alembic_version`) first gets the old hand-written create / migrate code,
which catches it up to the baseline, is checked against the models, then stamped; from there `upgrade head` applies
whatever came later. A new database is built by `upgrade head` alone.
"""

import logging
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlmodel import SQLModel

import felt_core.db.tables  # ruff: ignore[unused-import]  # registers every table on SQLModel.metadata

logger = logging.getLogger(__name__)

MIGRATIONS = Path(__file__).with_name("migrations")
BASELINE = "2026_11_11_baseline"


def alembic_config(db_path: str) -> Config:
    """Alembic's settings for one database - no alembic.ini, so nothing to keep in step."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("file_template", "%%(year)d_%%(month).2d_%%(day).2d_%%(slug)s")
    # a new revision comes out lint- and format-clean, like everything else in the repo
    cfg.set_section_option("post_write_hooks", "hooks", "ruff_fix, ruff_format")
    for hook, options in (
        ("ruff_fix", "check --fix REVISION_SCRIPT_FILENAME"),
        ("ruff_format", "format REVISION_SCRIPT_FILENAME"),
    ):
        cfg.set_section_option("post_write_hooks", f"{hook}.type", "module")
        cfg.set_section_option("post_write_hooks", f"{hook}.module", "ruff")
        cfg.set_section_option("post_write_hooks", f"{hook}.options", options)
    return cfg


def _tables(db_path: str) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _legacy_catch_up(db_path: str) -> None:
    """The hand-written schema code, run once on a pre-Alembic database (it's idempotent)."""
    from felt_core.brain import router
    from felt_core.core import asides, emails, incidents, marks
    from felt_core.db.schema import _create_schema
    from felt_core.dj import store

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        _create_schema(conn)
        for module in (marks, incidents, asides, emails):
            module._ensure(conn)
        conn.executescript(store._SCHEMA)
        conn.execute(router._SCHEMA)
        conn.commit()
    finally:
        conn.close()


def schema_differences(db_path: str) -> list:
    """What Alembic would change to make the live schema match the models; empty when they agree."""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            return compare_metadata(MigrationContext.configure(connection), SQLModel.metadata)
    finally:
        engine.dispose()


def migrate(db_path: str) -> None:
    """Upgrade felt.db to the newest schema, stamping a pre-Alembic one at the baseline first."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    cfg = alembic_config(db_path)
    tables = _tables(db_path)
    if tables and "alembic_version" not in tables:
        _legacy_catch_up(db_path)
        diff = schema_differences(db_path)
        if diff:
            logger.error("felt.db differs from the baseline models - stamped anyway, check: %s", diff)
        command.stamp(cfg, BASELINE)
        logger.info("felt.db: pre-Alembic schema stamped at %s", BASELINE)
    command.upgrade(cfg, "head")
