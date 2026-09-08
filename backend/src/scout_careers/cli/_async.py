"""One way to run a coroutine from a Typer command.

Every CLI entry point goes through :func:`run`. Calling ``asyncio.run`` directly
is a review failure, for a reason that is invisible until it bites:

``_default_engine`` is ``lru_cache``d for the life of the process, so the second
``asyncio.run`` in one command inherits a connection pool whose asyncpg
connections were created inside the *first* loop — which is now closed. SQLAlchemy
surfaces that as::

    RuntimeError: Task ... got Future ... attached to a different loop
    RuntimeError: Event loop is closed

`scout-careers source retire` hit exactly this: one ``asyncio.run`` to fetch the
preview and the posting count, a synchronous confirmation prompt between them,
then a second ``asyncio.run`` to perform the update. Both halves were correct;
the pool outliving the loop was not.

Disposing the engine after each run makes every ``asyncio.run`` start with a
fresh pool bound to its own loop. For a CLI that is free — the process makes a
handful of queries and exits — and it removes a whole class of bug rather than
the one instance of it. The long-running services (the API, the scheduler) hold
a single loop for their lifetime and are unaffected either way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from scout_careers.db.session import dispose_engine


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run one coroutine to completion and release the database engine.

    Args:
        coro: The coroutine to run.

    Returns:
        Whatever the coroutine returns.
    """

    async def _with_disposal() -> T:
        try:
            return await coro
        finally:
            # In the same loop that created the pool, so the connections are
            # closed by the loop that owns them.
            await dispose_engine()

    return asyncio.run(_with_disposal())


__all__ = ["run"]
