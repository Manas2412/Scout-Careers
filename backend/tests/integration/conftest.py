"""Harness for the database tests.

Two overrides of the repository-level ``conftest``, both deliberate:

- ``_no_network`` is replaced with a no-op. The repository fixture patches
  ``socket`` so that a unit test cannot reach an employer; these tests must
  reach ``localhost:5432``, and only that.
- Every test is marked ``integration`` and skipped when ``DATABASE_URL`` is
  unset, so a developer without a database still gets a green unit suite rather
  than a wall of connection errors.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "")

requires_postgres = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is not set; integration tests need a live Postgres",
)


@pytest.fixture(autouse=True)
def _no_network() -> Iterator[None]:
    """Override the repository-level socket block. These tests need Postgres."""
    yield


@pytest.fixture(scope="session")
def database_url() -> str:
    """The DSN under test, or a skip."""
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is not set")
    return DATABASE_URL


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    """An ``AsyncSession`` on a database the migrations have been applied to."""
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as open_session:
        yield open_session
    await engine.dispose()
