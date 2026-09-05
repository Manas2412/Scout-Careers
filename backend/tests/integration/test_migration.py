"""The migration runs forward and back.

Downgrades are written but only exercised in development (DATA_MODEL.md §11).
"Only exercised in development" still means exercised: a downgrade that has
never run is not a downgrade, it is a paragraph.

This test is deliberately synchronous. Alembic's ``env.py`` drives an async
engine through ``asyncio.run``, which cannot be called from inside a running
loop, so the schema is inspected through its own short-lived ``asyncio.run``
rather than by making the test a coroutine.
"""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from scout_careers.cli.main import alembic_config
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

EXPECTED_TABLES = {"company", "source", "job_posting", "run_log", "alembic_version"}
PHASE_1_TABLES = {"company", "source", "job_posting", "run_log"}


def _schema(database_url: str) -> tuple[set[str], set[str]]:
    """Return the table names, and ``job_posting``'s columns when it exists."""

    async def _read() -> tuple[set[str], set[str]]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(_inspect)
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def _inspect(connection: object) -> tuple[set[str], set[str]]:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    columns: set[str] = set()
    if "job_posting" in tables:
        columns = {column["name"] for column in inspector.get_columns("job_posting")}
    return tables, columns


def test_upgrade_then_downgrade_is_clean(database_url: str) -> None:
    config = alembic_config()

    try:
        command.upgrade(config, "head")
        tables, columns = _schema(database_url)
        assert tables >= EXPECTED_TABLES

        # The counter the two-run close rule needs; adding it later would mean
        # backfilling it from run history we do not keep.
        assert "missed_runs" in columns
        assert {"content_hash", "first_seen_at", "last_seen_at", "closed_at"} <= columns
        assert {"filtered_out", "filter_reason"} <= columns

        command.downgrade(config, "base")
        remaining, _ = _schema(database_url)
        assert not (remaining & PHASE_1_TABLES)
    finally:
        # Leave the database at head whatever happened, so a failure here does
        # not strand every other integration test on an empty schema.
        command.upgrade(config, "head")
