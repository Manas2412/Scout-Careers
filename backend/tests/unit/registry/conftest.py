"""Fakes for the source-administration tests. No database, no network.

The session fake answers the three questions the service actually asks — "give
me this row", "count these rows", "run this statement" — and records the
mutating calls so a test can assert that a retire deleted nothing. Anything more
would be a query planner testing itself (the same reasoning as
``tests/unit/ingest/conftest.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict
from sqlalchemy.dialects import postgresql

from scout_careers.common.types import AtsType
from scout_careers.db.models import Company, Source
from scout_careers.sources.base import ProbeResult

RUN_AT = datetime(2026, 9, 4, 2, 30, tzinfo=UTC)


class FakeResult:
    """What ``session.execute`` returns here."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeSession:
    """A stand-in for ``AsyncSession`` that records what was asked of it."""

    def __init__(
        self,
        *,
        rows: dict[Any, Any] | None = None,
        select_rows: list[list[Any]] | None = None,
        scalars: list[Any] | None = None,
    ) -> None:
        self.rows = dict(rows or {})
        self._select_rows = list(select_rows or [])
        self._scalars = list(scalars or [])
        self.statements: list[Any] = []
        self.deleted: list[Any] = []
        self.flushes = 0

    async def get(self, _model: Any, ident: Any) -> Any:
        return self.rows.get(ident)

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self._select_rows.pop(0) if self._select_rows else [])

    async def scalar(self, statement: Any, *_args: Any, **_kwargs: Any) -> Any:
        self.statements.append(statement)
        return self._scalars.pop(0) if self._scalars else 0

    async def delete(self, instance: Any) -> None:
        self.deleted.append(instance)

    async def flush(self) -> None:
        self.flushes += 1


def compiled(statement: Any) -> str:
    """Render a statement as the SQL Postgres would receive.

    The filters in ``sources_query`` are the thing that can be wrong, and
    compiling the statement is how they are checked without a database.
    """
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def make_company(company_id: int = 1, *, slug: str = "anysphere") -> Company:
    """Build a ``Company`` ORM instance without a database behind it."""
    return Company(id=company_id, slug=slug, name=slug.title())


def make_source(
    source_id: int = 1,
    *,
    company_id: int = 1,
    adapter: AtsType = AtsType.ASHBY,
    config: dict[str, Any] | None = None,
    enabled: bool = True,
    last_status: str | None = "http_error",
    last_error: str | None = "Board token not found",
    consecutive_failures: int = 3,
) -> Source:
    """Build a ``Source`` ORM instance without a database behind it."""
    return Source(
        id=source_id,
        company_id=company_id,
        adapter=adapter,
        config=config if config is not None else {"board_name": "anysphere"},
        enabled=enabled,
        poll_interval_minutes=1440,
        last_run_at=RUN_AT,
        last_status=last_status,
        last_error=last_error,
        consecutive_failures=consecutive_failures,
    )


class DeniedHostConfig(BaseModel):
    """Config for :class:`DeniedHostAdapter`: a host straight from the row."""

    model_config = ConfigDict(extra="forbid")

    host: str


class DeniedHostAdapter:
    """An adapter whose board URL comes out of its config.

    No Phase 1 adapter can point at a never-fetch host — all three build their
    URL from a hard-coded API host — so proving the policy gate in
    ``probe_source`` needs an adapter that can. Workday's real config *is* shaped
    this way, which is why this stands in for it.
    """

    name: ClassVar[AtsType] = AtsType.WORKDAY
    config_model: ClassVar[type[BaseModel]] = DeniedHostConfig
    fidelity_rank: ClassVar[int] = 70
    default_poll_interval_minutes: ClassVar[int] = 1440
    requires_detail_fetch: ClassVar[bool] = False
    URL_TEMPLATE: ClassVar[str] = "{host}/careers/jobs"

    #: Set when ``probe()`` runs. It must stay False for a denied host.
    probed: ClassVar[bool] = False

    def __init__(self, *, source_id: int, config: BaseModel, http: Any) -> None:
        self.source_id = source_id
        self.config = config
        self._http = http

    @classmethod
    def parse_config(cls, raw: dict[str, Any]) -> DeniedHostConfig:
        return DeniedHostConfig.model_validate(raw)

    async def probe(self) -> ProbeResult:
        type(self).probed = True
        return ProbeResult(reachable=True, latency_ms=1)

    async def fetch(self, *, since: Any = None) -> Any:  # pragma: no cover - unused
        del since
        return
        yield

    async def aclose(self) -> None:
        return None

    def describe(self) -> str:
        return "Denied · board"
