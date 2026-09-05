"""Async engine, session factory and the one blessed session scope.

One ``AsyncSession`` per request or per task; never module-level, never shared
across tasks (DEVELOPMENT.md §4.2). ``session_scope`` is the only place a commit
is paired with a rollback, so no caller has to remember both.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from scout_careers.common.config import Settings, get_settings


def _install_connect_timeouts(engine: AsyncEngine, settings: Settings) -> None:
    """Apply ``statement_timeout`` and ``lock_timeout`` to every new connection.

    Set on connect rather than per transaction so that no query path can forget
    them: a runaway full-text query must not be able to hold a pooled
    connection, and a query must not wait indefinitely behind a migration's
    lock (CONFIGURATION.md §3).

    Args:
        engine: The async engine whose pooled connections are being configured.
        settings: Supplies the two millisecond budgets.
    """
    statement_timeout_ms = int(settings.database_statement_timeout_ms)
    lock_timeout_ms = int(settings.database_lock_timeout_ms)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_timeouts(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            # Postgres does not accept bind parameters in SET; both values are
            # ints cast above, so there is no interpolation of untrusted text.
            cursor.execute(f"SET statement_timeout = {statement_timeout_ms}")
            cursor.execute(f"SET lock_timeout = {lock_timeout_ms}")
        finally:
            cursor.close()


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Build the async engine with the configured pool.

    Args:
        settings: Configuration; resolved from the environment when omitted.

    Returns:
        A configured ``AsyncEngine``. The caller owns disposal.
    """
    resolved = settings or get_settings()
    engine = create_async_engine(
        str(resolved.database_url),
        echo=resolved.database_echo,
        pool_size=resolved.database_pool_size,
        max_overflow=resolved.database_max_overflow,
        pool_timeout=resolved.database_pool_timeout_s,
        pool_recycle=resolved.database_pool_recycle_s,
        pool_pre_ping=True,
        future=True,
    )
    _install_connect_timeouts(engine, resolved)
    return engine


def async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to ``engine``.

    Args:
        engine: The engine sessions are bound to.

    Returns:
        A session maker producing ``AsyncSession`` instances that do not expire
        attributes on commit — the caller usually reads the object afterwards.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@lru_cache(maxsize=1)
def _default_engine() -> AsyncEngine:
    return create_engine()


@lru_cache(maxsize=1)
def _default_factory() -> async_sessionmaker[AsyncSession]:
    return async_session_factory(_default_engine())


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, creating the engine on first use.

    Returns:
        The cached session maker.
    """
    return _default_factory()


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """Yield a session that commits on success and rolls back on any exception.

    Args:
        factory: Session maker to use; the process-wide one when omitted.

    Yields:
        An ``AsyncSession`` bound to a single unit of work.
    """
    maker = factory or get_session_factory()
    session = maker()
    try:
        yield session
    except BaseException:
        await session.rollback()
        raise
    else:
        await session.commit()
    finally:
        await session.close()


async def dispose_engine() -> None:
    """Dispose the process-wide engine and clear the caches. Used at shutdown."""
    if _default_engine.cache_info().currsize:
        await _default_engine().dispose()
    _default_factory.cache_clear()
    _default_engine.cache_clear()


__all__ = [
    "async_session_factory",
    "create_engine",
    "dispose_engine",
    "get_session_factory",
    "session_scope",
]
