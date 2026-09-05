"""Fakes for the ingest tests. No database, no network, no Redis.

The ingest layer is the only layer that persists, so testing it offline means
standing in for a session. The fakes here are deliberately thin: they answer the
two questions the code under test actually asks — "what rows are there" and "what
statements did you execute" — and nothing else. A fake that grew a query planner
would be testing itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, Update

from scout_careers.common.types import AtsType
from scout_careers.db.models import Source
from scout_careers.ingest.runner import SourceOutcome
from scout_careers.sources.base import RawPosting, SourceResult

RUN_START = datetime(2026, 9, 5, 2, 30, tzinfo=UTC)


@dataclass
class Row:
    """A stand-in for a SQLAlchemy ``Row`` with named attributes."""

    id: str = ""
    missed_runs: int = 0
    external_id: str = ""
    content_hash: str = ""


class FakeResult:
    """What ``session.execute`` returns in these tests."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeSession:
    """Records every statement and answers selects from a canned queue."""

    def __init__(self, select_rows: list[list[Any]] | None = None) -> None:
        self._select_rows = list(select_rows or [])
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.commits = 0
        self.gets: dict[Any, Any] = {}

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> FakeResult:
        self.statements.append(statement)
        if isinstance(statement, Select) and self._select_rows:
            return FakeResult(self._select_rows.pop(0))
        return FakeResult()

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def get(self, _model: Any, ident: Any) -> Any:
        return self.gets.get(ident)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def scalar(self, _statement: Any) -> Any:
        return 0

    # -- assertions helpers -------------------------------------------------

    @property
    def updates(self) -> list[Update]:
        """Every UPDATE that was executed."""
        return [stmt for stmt in self.statements if isinstance(stmt, Update)]


class FakeLock:
    """A run lock whose behaviour the test dictates."""

    def __init__(self, *, acquired: bool = True, error: BaseException | None = None) -> None:
        self.acquired = acquired
        self.error = error
        self.released = False

    async def acquire(self) -> bool:
        if self.error is not None:
            raise self.error
        return self.acquired

    async def release(self) -> None:
        self.released = True


class FakeAdapter:
    """An adapter that yields what the test says, or fails how the test says."""

    def __init__(
        self,
        *,
        postings: list[RawPosting] | None = None,
        raises: BaseException | None = None,
        describe: str = "fake · board",
        hang_after: float | None = None,
    ) -> None:
        self._postings = postings or []
        self._raises = raises
        self._describe = describe
        self._hang_after = hang_after
        self.skipped: dict[str, int] = {}
        self.closed = False

    def describe(self) -> str:
        return self._describe

    async def fetch(self, *, since: datetime | None = None) -> Any:
        del since
        if self._raises is not None:
            raise self._raises
        for posting in self._postings:
            yield posting
        if self._hang_after is not None:
            await asyncio.sleep(self._hang_after)

    async def aclose(self) -> None:
        self.closed = True


class RecordingPersister:
    """Stands in for the per-source transaction, and remembers what it was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, list[RawPosting], SourceResult]] = []

    async def __call__(
        self,
        *,
        source: Any,
        postings: Any,
        result: SourceResult,
        settings: Any,
    ) -> SourceOutcome:
        del settings
        buffered = list(postings)
        self.calls.append((source.id, buffered, result))
        result.new = len(buffered)
        return SourceOutcome(result=result)


def make_source(
    source_id: int,
    *,
    company_id: int = 1,
    adapter: AtsType = AtsType.GREENHOUSE,
    config: dict[str, Any] | None = None,
    enabled: bool = True,
) -> Source:
    """Build a ``Source`` ORM instance without a database behind it."""
    return Source(
        id=source_id,
        company_id=company_id,
        adapter=adapter,
        config=config or {"board_token": f"board-{source_id}"},
        enabled=enabled,
        poll_interval_minutes=1440,
        consecutive_failures=0,
    )


def make_posting(**overrides: Any) -> RawPosting:
    """Build a ``RawPosting`` with sensible defaults."""
    values: dict[str, Any] = {
        "external_id": "1",
        "url": "https://boards.example.com/jobs/1",
        "title": "Senior Backend Engineer",
        "description_text": "Build and run distributed services.",
        "location_city": "Bengaluru",
        "location_country": "IN",
    }
    values.update(overrides)
    return RawPosting(**values)
