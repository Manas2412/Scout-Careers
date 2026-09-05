"""The adapter contract: the protocol, the DTOs and the per-adapter config models.

An adapter is a pure function from a validated config object to a stream of
``RawPosting`` DTOs, plus a cheap liveness probe. That is the whole contract
(SOURCE_ADAPTERS.md §1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from scout_careers.common.types import AtsType, SourceStatus

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle at runtime
    from scout_careers.sources.http import SourceHttpClient

EmploymentType = Literal["full_time", "part_time", "contract", "internship", "temporary", "unknown"]
Seniority = Literal[
    "intern",
    "entry",
    "mid",
    "senior",
    "staff",
    "principal",
    "manager",
    "director",
    "executive",
    "unknown",
]


class RawPosting(BaseModel):
    """The DTO crossing the ``sources/`` → ``ingest/`` boundary.

    Raw in the sense that it has not been deduplicated, filtered, scored or
    persisted — but already normalised: field names, HTML stripping, location
    parsing and timestamp handling are the adapter's job.

    ``extra="forbid"`` and ``frozen=True`` are both load-bearing. Forbidding
    extras turns "the vendor added a field and someone quietly started
    depending on it" into a test failure. Freezing means ``ingest/`` cannot
    mutate an adapter's output in place, which is what keeps fixture-replay
    tests meaningful.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- identity -------------------------------------------------------
    external_id: Annotated[str, Field(min_length=1, max_length=256)]
    """Stable upstream ID. Unique within a source, forever."""

    url: HttpUrl
    """Public, human-openable posting URL. This is what the operator clicks."""

    # --- content --------------------------------------------------------
    title: Annotated[str, Field(min_length=1, max_length=512)]
    description_html: str | None = None
    description_text: Annotated[str, Field(min_length=1)]
    """Already HTML-stripped and whitespace-normalised. Never None: a posting
    with no description is dropped by the adapter with a WARN, because an empty
    JD cannot be extracted or scored and would burn a filter slot."""

    # --- classification -------------------------------------------------
    department: str | None = None
    location_raw: str | None = None
    location_city: str | None = None
    location_country: str | None = None  # ISO-3166 alpha-2, upper case
    is_remote: bool = False
    employment_type: EmploymentType = "unknown"
    seniority_guess: Seniority = "unknown"

    # --- time -----------------------------------------------------------
    posted_at: datetime | None = None
    """UTC, tz-aware. None when upstream gives only a fuzzy string it cannot
    resolve. Never fabricated as now()."""

    # --- provenance -----------------------------------------------------
    raw: dict[str, Any] = Field(default_factory=dict)
    """The upstream object, trimmed to fields we might later want. Persisted to
    ``job_posting.raw``. Must not contain auth material."""

    @field_validator("location_country")
    @classmethod
    def _iso2(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) != 2 or not v.isalpha():
            raise ValueError("location_country must be ISO-3166 alpha-2")
        return v.upper()


class ProbeResult(BaseModel):
    """One cheap liveness check, returned verbatim by the detect/test endpoints.

    ``detail`` is a curated message — "Board token not found", not an upstream
    stack trace — because it is rendered in the UI and upstream bodies are
    untrusted input.
    """

    model_config = ConfigDict(extra="forbid")

    reachable: bool
    sample_count: int = 0
    latency_ms: int
    http_status: int | None = None
    detail: str | None = None
    company_name_guess: str | None = None


@runtime_checkable
class SourceAdapter(Protocol):
    """One employer-facing job source.

    Implementations are constructed per run, used once, and closed. They are
    not thread-safe and must not be cached across runs — the shared HTTP client
    and the rate-limit lease are bound at construction.
    """

    # ---- class-level identity -------------------------------------------
    name: ClassVar[AtsType]
    """Matches the ``ats_type`` enum value and the ``source.adapter`` column."""

    config_model: ClassVar[type[BaseModel]]
    """Pydantic model that validates ``source.config`` JSONB for this adapter."""

    fidelity_rank: ClassVar[int]
    """0-100. Higher wins when collapsing cross-source duplicates."""

    default_poll_interval_minutes: ClassVar[int]
    """Seeded into ``source.poll_interval_minutes`` on creation."""

    requires_detail_fetch: ClassVar[bool]
    """True when the list endpoint omits the description."""

    # ---- construction ----------------------------------------------------
    def __init__(
        self,
        *,
        source_id: int,
        config: BaseModel,
        http: SourceHttpClient,
    ) -> None: ...

    # ---- lifecycle -------------------------------------------------------
    @classmethod
    def parse_config(cls, raw: dict[str, Any]) -> BaseModel:
        """Validate raw JSONB into ``config_model``.

        Raises ``AdapterConfigError`` (mapped to HTTP 422) on failure. Called by
        the registry on source creation AND at the top of every run, so a config
        that rotted since it was saved fails loudly, not silently.
        """

    async def probe(self) -> ProbeResult:
        """One cheap request. Confirms the endpoint exists and returns postings.

        Must complete within ``settings.source_probe_timeout_s`` and must never
        page.
        """

    def fetch(self, *, since: datetime | None = None) -> AsyncIterator[RawPosting]:
        """Yield every currently-listed posting for this source.

        ``since`` is advisory: adapters whose upstream supports server-side date
        filtering use it; the rest ignore it and ``ingest/`` does change
        detection by ``content_hash``. Adapters MUST NOT swallow their own
        exceptions — the runner owns failure isolation.
        """

    async def aclose(self) -> None:
        """Release adapter-local resources. The HTTP client is NOT owned here."""

    # ---- presentation ----------------------------------------------------
    def describe(self) -> str:
        """Short human string for the Companies page and the digest."""


# ---------------------------------------------------------------------------
# Per-adapter config models (SOURCE_ADAPTERS.md §2.5)
#
# The pattern on any field interpolated into a URL is a security control, not
# tidiness: config must not be able to point an adapter at an arbitrary host.
# §4.7's never-scrape check backstops it.
# ---------------------------------------------------------------------------


class GreenhouseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board_token: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]


class LeverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]


class AshbyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board_name: Annotated[str, Field(min_length=1, max_length=64)]
    include_compensation: bool = True


class WorkdayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: Annotated[str, Field(pattern=r"^[a-z0-9.-]+\.myworkdayjobs\.com$")]
    tenant: Annotated[str, Field(min_length=1, max_length=64)]
    site: Annotated[str, Field(min_length=1, max_length=64)]
    locale: str = "en-US"
    applied_facets: dict[str, list[str]] = Field(default_factory=dict)
    max_pages: Annotated[int, Field(ge=1, le=200)] = 50


class SmartRecruitersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: Annotated[str, Field(min_length=1, max_length=64)]


class WorkableConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")]


class RecruiteeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]


class KeywordSearchConfig(BaseModel):
    """Shared by google / amazon / microsoft: one employer, huge board, so the
    query is the config."""

    model_config = ConfigDict(extra="forbid")
    queries: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=lambda: ["IN"])
    max_results_per_query: Annotated[int, Field(ge=1, le=2000)] = 500


class MailAlertConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = "job-alerts"
    senders: list[str] = Field(
        default_factory=lambda: [
            "jobalerts-noreply@linkedin.com",
            "jobs-listings@linkedin.com",
            "info@naukri.com",
            "alert@indeed.com",
            "noreply@indeed.com",
        ]
    )
    lookback_hours: Annotated[int, Field(ge=1, le=168)] = 26


# ---------------------------------------------------------------------------
# Mail access, as seen from sources/
# ---------------------------------------------------------------------------


class MailMessage(BaseModel):
    """A delivered message, as much of it as an alert parser needs.

    Deliberately not the Gmail API shape: ``sources/`` must not depend on the
    transport, and ``mail/`` must not be imported from here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: Annotated[str, Field(min_length=1)]
    sender: str
    subject: str = ""
    received_at: datetime
    html_body: str | None = None
    text_body: str | None = None


@runtime_checkable
class MailReader(Protocol):
    """The single capability the ``mail_alert`` adapter needs from ``mail/``.

    Declared here rather than imported from ``mail/`` so the layering rule
    holds: ``sources/`` depends on a protocol it owns, and ``ingest/`` injects a
    concrete Gmail-backed reader at run construction.
    """

    async def list_recent_messages(
        self,
        *,
        label: str,
        senders: Sequence[str],
        since: datetime,
    ) -> Sequence[MailMessage]:
        """Return messages in ``label`` from ``senders`` received at or after ``since``."""


# ---------------------------------------------------------------------------
# The per-source run result (SOURCE_ADAPTERS.md §10.3)
# ---------------------------------------------------------------------------


class SourceResult(BaseModel):
    """One entry in ``run_log.source_results``.

    ``error`` is singular, curated, non-sensitive and capped at 500 characters.
    It is never a raw upstream body and never a stack trace — it is rendered in
    the digest and the Settings health table, and upstream bodies are untrusted.
    The stack trace goes to structured logs, keyed by ``run_id`` and
    ``source_id``.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: int
    company_id: int
    adapter: AtsType
    describe: str
    status: SourceStatus
    fetched: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    duration_ms: int = 0
    requests: int = 0
    retries: int = 0
    rate_limit_wait_ms: int = 0
    error: Annotated[str | None, Field(max_length=500)] = None
    error_code: str | None = None

    @classmethod
    def crashed(
        cls,
        *,
        source_id: int,
        company_id: int,
        adapter: AtsType,
        describe: str,
        exc: BaseException,
        duration_ms: int = 0,
    ) -> SourceResult:
        """Build the result for a failure in the runner itself.

        Reached only when ``fetch_one``'s own classifier raised — which should be
        impossible. It is recorded rather than raised so that invariant 5 holds
        even when the code that upholds invariant 5 is the thing that broke.

        Args:
            source_id: The source being run.
            company_id: Its company.
            adapter: The adapter type.
            describe: The adapter's human description.
            exc: The exception that escaped.
            duration_ms: Elapsed time before the crash.

        Returns:
            A ``SourceResult`` with status ``error`` and a type-only message —
            the exception's text may quote an upstream body, so it is not used.
        """
        return cls(
            source_id=source_id,
            company_id=company_id,
            adapter=adapter,
            describe=describe,
            status=SourceStatus.ERROR,
            duration_ms=duration_ms,
            error=f"runner crashed: {type(exc).__name__}",
            error_code="adapter.unknown",
        )


__all__ = [
    "AshbyConfig",
    "EmploymentType",
    "GreenhouseConfig",
    "KeywordSearchConfig",
    "LeverConfig",
    "MailAlertConfig",
    "MailMessage",
    "MailReader",
    "ProbeResult",
    "RawPosting",
    "RecruiteeConfig",
    "Seniority",
    "SmartRecruitersConfig",
    "SourceAdapter",
    "SourceResult",
    "WorkableConfig",
    "WorkdayConfig",
]
