"""Greenhouse job-board adapter (SOURCE_ADAPTERS.md §5.1).

One request, whole board, full descriptions embedded. Fidelity 90.

The four caveats from §5.1, each of which is a line of code here:

- ``content`` is **HTML-entity-escaped**. It is unescaped exactly once on each
  path: :func:`html.unescape` for ``description_html``, and ``html_to_text``'s
  own leading unescape applied to the *original* string for
  ``description_text``. Skipping it turns every description into a wall of
  ``&lt;p&gt;`` — the single most common Greenhouse integration bug. Doing it
  twice would decode a literal ``&amp;`` the recruiter typed.
- There is **no ``created_at``**. ``posted_at`` comes from ``updated_at``, which
  moves whenever a recruiter edits the post, so it drifts later over a role's
  life. It is still the best available signal, and ``job_posting.first_seen_at``
  is what recency ranking actually keys on. Recorded here so nobody later
  "fixes" the mapping.
- The human URL may live on ``job-boards.greenhouse.io`` while the API stays on
  ``boards-api.greenhouse.io``. Only the API host is constructed here; the
  posting URL is taken from ``absolute_url`` and is never assembled.
- A board published ``embed``-only answers 404. That is a detection failure, not
  a retry case: ``probe`` reports it as unreachable with a curated detail, and
  ``fetch`` lets ``UpstreamHttpError`` — whose ``error_code`` is already
  ``adapter.board_not_found`` — propagate to the runner. The shared client
  already refuses to retry a 4xx.
"""

from __future__ import annotations

import asyncio
import html as html_entities
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scout_careers.common.errors import AdapterConfigError, ScoutError, UpstreamHttpError
from scout_careers.common.logging import get_logger
from scout_careers.common.types import AtsType
from scout_careers.sources._shared import (
    NullIsEmptyList,
    bound_description,
    company_guess,
    config_error,
    drift_error,
    elapsed_ms,
    probe_detail,
)
from scout_careers.sources.base import GreenhouseConfig, ProbeResult, RawPosting
from scout_careers.sources.http import SourceHttpClient
from scout_careers.sources.normalise import (
    html_to_text,
    infer_seniority,
    map_employment_type,
    parse_iso_datetime,
    parse_location,
)

log = get_logger(__name__)

HTTP_OK = 200

#: The metadata entry Greenhouse boards conventionally use for the contract type.
EMPLOYMENT_TYPE_METADATA_KEY = "employment type"


# ---------------------------------------------------------------------------
# The upstream page shape (§12 step 5)
#
# ``extra="ignore"`` rather than ``forbid``: Greenhouse adds fields to this
# payload without warning, and a new optional field is not drift. A *missing
# required* field is, and that is exactly what these models refuse.
# ---------------------------------------------------------------------------


class _GreenhouseNamed(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    name: str | None = None
    parent_id: int | None = None


class _GreenhouseOffice(_GreenhouseNamed):
    location: str | None = None


class _GreenhouseLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None


class _GreenhouseMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    value: Any = None


class _GreenhouseJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    absolute_url: str
    title: str
    content: str | None = None
    updated_at: str | None = None
    internal_job_id: int | None = None
    requisition_id: str | None = None
    location: _GreenhouseLocation | None = None
    # Every defaulted list tolerates an explicit null — Greenhouse sends
    # `"metadata": null` on boards with no custom fields (_shared.py).
    departments: Annotated[list[_GreenhouseNamed], NullIsEmptyList] = Field(default_factory=list)
    offices: Annotated[list[_GreenhouseOffice], NullIsEmptyList] = Field(default_factory=list)
    metadata: Annotated[list[_GreenhouseMetadata], NullIsEmptyList] = Field(default_factory=list)


class _GreenhousePage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jobs: list[_GreenhouseJob]


class GreenhouseAdapter:
    """Reads one Greenhouse board in a single request."""

    name: ClassVar[AtsType] = AtsType.GREENHOUSE
    config_model: ClassVar[type[BaseModel]] = GreenhouseConfig
    fidelity_rank: ClassVar[int] = 90
    default_poll_interval_minutes: ClassVar[int] = 1440
    requires_detail_fetch: ClassVar[bool] = False

    #: The only host this adapter constructs. The human URL is never assembled.
    API_HOST: ClassVar[str] = "https://boards-api.greenhouse.io"
    #: What is logged. Never the interpolated URL — a board token is not log-safe.
    URL_TEMPLATE: ClassVar[str] = "boards-api.greenhouse.io/v1/boards/{board_token}/jobs"

    def __init__(
        self,
        *,
        source_id: int,
        config: BaseModel,
        http: SourceHttpClient,
    ) -> None:
        if not isinstance(config, GreenhouseConfig):
            raise AdapterConfigError("GreenhouseAdapter requires a GreenhouseConfig")
        self.source_id = source_id
        self.config = config
        self._http = http
        #: Per-run skip counts; the runner copies them into
        #: ``SourceResult.skipped`` (§10.3).
        self.skipped: dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def parse_config(cls, raw: dict[str, Any]) -> GreenhouseConfig:
        """Validate ``source.config`` JSONB into a ``GreenhouseConfig``.

        Args:
            raw: The stored config object.

        Returns:
            The validated config.

        Raises:
            AdapterConfigError: When the config does not validate. The pattern
                on ``board_token`` is a security control, not tidiness: config
                must not be able to point this adapter at an arbitrary host.
        """
        try:
            return GreenhouseConfig.model_validate(raw)
        except ValidationError as exc:
            raise config_error("greenhouse", exc) from exc

    async def probe(self) -> ProbeResult:
        """Fetch the board once and report whether it exists and has postings.

        Returns:
            A ``ProbeResult``. ``detail`` is curated — never an upstream body,
            which is untrusted input rendered directly in the UI.
        """
        started = time.monotonic()
        try:
            page = await asyncio.wait_for(
                self._get_board(), timeout=self._http.settings.source_probe_timeout_s
            )
        except UpstreamHttpError as exc:
            return ProbeResult(
                reachable=False,
                latency_ms=elapsed_ms(started),
                http_status=exc.status_code,
                detail=probe_detail(exc.status_code),
            )
        except (ScoutError, TimeoutError) as exc:
            return ProbeResult(
                reachable=False,
                latency_ms=elapsed_ms(started),
                detail=f"Board unreachable ({type(exc).__name__})",
            )

        return ProbeResult(
            reachable=True,
            sample_count=len(page.jobs),
            latency_ms=elapsed_ms(started),
            http_status=HTTP_OK,
            company_name_guess=company_guess(self.config.board_token),
        )

    async def fetch(self, *, since: datetime | None = None) -> AsyncIterator[RawPosting]:
        """Yield every currently-listed posting on the board.

        Args:
            since: Ignored. Greenhouse has no server-side date filter, so
                ``ingest/`` does change detection by ``content_hash``.

        Yields:
            One ``RawPosting`` per listed job that has a usable description.

        Raises:
            UpstreamHttpError: On 404 — the board token is wrong or the board is
                ``embed``-only — and on any other 4xx.
            SchemaDriftError: When the response does not match the page model.
        """
        del since
        # The whole page is validated before anything is yielded: a drifted item
        # must not leave a half-ingested board behind it (§4.4).
        page = await self._get_board()
        for job in page.jobs:
            posting = self._to_posting(job)
            if posting is not None:
                yield posting

    async def aclose(self) -> None:
        """Nothing adapter-local to release; the HTTP client is not owned here."""

    def describe(self) -> str:
        """Return the short human string used on the Companies page and digest."""
        return f"Greenhouse · {self.config.board_token}"

    # -- internals ---------------------------------------------------------

    def _url(self) -> str:
        return f"{self.API_HOST}/v1/boards/{self.config.board_token}/jobs"

    async def _get_board(self) -> _GreenhousePage:
        payload = await self._http.get_json(
            self._url(),
            params={"content": "true"},
            url_template=self.URL_TEMPLATE,
        )
        try:
            return _GreenhousePage.model_validate(payload)
        except ValidationError as exc:
            raise drift_error(self.URL_TEMPLATE, exc) from exc

    def _to_posting(self, job: _GreenhouseJob) -> RawPosting | None:
        # Exactly one unescape on each path. html_to_text receives the ORIGINAL
        # content because it unescapes internally; unescaping first and then
        # handing it the result would decode entities twice.
        description_html = html_entities.unescape(job.content) if job.content else None
        description_text = html_to_text(job.content)
        if not description_text:
            self._count("no_description")
            log.warning(
                "posting_dropped_no_description",
                source_id=self.source_id,
                adapter=self.name.value,
                external_id=str(job.id),
            )
            return None

        bounded, truncated = bound_description(
            description_text, self._http.settings.max_description_chars
        )

        location_raw = job.location.name if job.location else None
        parsed = parse_location(location_raw)
        office_remote = any(
            office.name is not None and "remote" in office.name.lower() for office in job.offices
        )

        raw: dict[str, Any] = {
            "id": job.id,
            "internal_job_id": job.internal_job_id,
            "requisition_id": job.requisition_id,
            "updated_at": job.updated_at,
            "departments": [d.model_dump() for d in job.departments],
            "offices": [o.model_dump() for o in job.offices],
            "metadata": [m.model_dump() for m in job.metadata],
        }
        if truncated:
            raw["description_truncated"] = True

        return RawPosting(
            # The job-board post ID, never internal_job_id: that is the
            # requisition, and it is shared across several posts for one
            # multi-location role (§2.3 rule 1).
            external_id=str(job.id),
            url=job.absolute_url,
            title=job.title,
            description_html=description_html,
            description_text=bounded,
            department=_deepest_department(job.departments),
            location_raw=location_raw,
            location_city=parsed.city,
            location_country=parsed.country,
            is_remote=parsed.is_remote or office_remote,
            employment_type=map_employment_type(_employment_metadata(job.metadata)),
            seniority_guess=infer_seniority(job.title),
            # From updated_at, with the drift caveat in the module docstring.
            # Never fabricated as now().
            posted_at=parse_iso_datetime(job.updated_at),
            raw=raw,
        )

    def _count(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _deepest_department(departments: list[_GreenhouseNamed]) -> str | None:
    """Return the most specific department name.

    Greenhouse nests departments through ``parent_id``. The deepest one — a
    child — is the specific team; the root is usually just "Engineering".

    Args:
        departments: The job's department objects.

    Returns:
        The name of the deepest named department, or ``None``.
    """
    named = [d for d in departments if d.name]
    if not named:
        return None
    for department in named:
        if department.parent_id is not None:
            return department.name
    return named[0].name


def _employment_metadata(metadata: list[_GreenhouseMetadata]) -> str | None:
    """Return the ``Employment Type`` metadata value, if the board sets one.

    Args:
        metadata: The job's metadata entries.

    Returns:
        The raw upstream string, for ``map_employment_type``, or ``None``.
    """
    for entry in metadata:
        if entry.name and entry.name.strip().lower() == EMPLOYMENT_TYPE_METADATA_KEY:
            value = entry.value
            if isinstance(value, str):
                return value
            if isinstance(value, list) and value and isinstance(value[0], str):
                return value[0]
    return None


__all__ = ["EMPLOYMENT_TYPE_METADATA_KEY", "GreenhouseAdapter"]
