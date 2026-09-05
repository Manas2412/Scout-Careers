"""Ashby job-board adapter (SOURCE_ADAPTERS.md §5.3).

Fidelity 90 — the cleanest text of any adapter. One request, an envelope with a
``jobs`` array, a genuinely plain ``descriptionPlain``, an explicit ``isRemote``
and real ISO timestamps. Nothing has to be reconstructed.

The caveats from §5.3:

- **``isListed: false`` postings are skipped.** Ashby returns unlisted postings
  on this endpoint: drafts and confidential searches. Yielding them would put
  roles in the queue that the employer has not published. They are dropped and
  counted under ``skipped["unlisted"]``, which the runner copies into the source
  result — a number the operator can see rather than a silent omission.
- ``descriptionPlain`` is already clean, and still goes through the §9.1
  normalisation: NFKC, zero-width stripping and whitespace collapse are what
  keep ``content_hash`` stable when a recruiter re-pastes the same text from a
  different editor.
- The **compensation block** is the only place any adapter reliably surfaces
  salary. It is kept in ``raw`` and rendered on the posting page. It is never
  used in scoring — a salary band says nothing about requirement coverage.

Auth: none. Ashby's authenticated ``/api/*`` endpoints exist and are not used;
we have no API key and do not need one.
"""

from __future__ import annotations

import asyncio
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
    NullIsFalse,
    NullIsTrue,
    bound_description,
    company_guess,
    config_error,
    drift_error,
    elapsed_ms,
    probe_detail,
)
from scout_careers.sources.base import AshbyConfig, ProbeResult, RawPosting
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


# ---------------------------------------------------------------------------
# The upstream page shape (§12 step 5)
# ---------------------------------------------------------------------------


class _AshbySecondaryLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    location: str | None = None
    address: dict[str, Any] | None = None


class _AshbyJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    jobUrl: str  # noqa: N815 - upstream spelling
    applyUrl: str | None = None  # noqa: N815 - upstream spelling
    department: str | None = None
    team: str | None = None
    employmentType: str | None = None  # noqa: N815 - upstream spelling
    location: str | None = None
    # Defaulted fields tolerate an explicit null: Ashby sends `isRemote: null`
    # where the recruiter left it unset, and `secondaryLocations: null` on
    # single-location postings (_shared.py).
    secondaryLocations: Annotated[  # noqa: N815 - upstream spelling
        list[_AshbySecondaryLocation], NullIsEmptyList
    ] = Field(default_factory=list)
    publishedAt: str | None = None  # noqa: N815 - upstream spelling
    isListed: Annotated[bool, NullIsTrue] = True  # noqa: N815 - upstream spelling
    isRemote: Annotated[bool, NullIsFalse] = False  # noqa: N815 - upstream spelling
    descriptionHtml: str | None = None  # noqa: N815 - upstream spelling
    descriptionPlain: str | None = None  # noqa: N815 - upstream spelling
    compensation: dict[str, Any] | None = None


class _AshbyPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    apiVersion: str | None = None  # noqa: N815 - upstream spelling
    jobs: list[_AshbyJob]


class AshbyAdapter:
    """Reads one Ashby job board in a single request."""

    name: ClassVar[AtsType] = AtsType.ASHBY
    config_model: ClassVar[type[BaseModel]] = AshbyConfig
    fidelity_rank: ClassVar[int] = 90
    default_poll_interval_minutes: ClassVar[int] = 1440
    requires_detail_fetch: ClassVar[bool] = False

    API_HOST: ClassVar[str] = "https://api.ashbyhq.com"
    URL_TEMPLATE: ClassVar[str] = "api.ashbyhq.com/posting-api/job-board/{board_name}"

    def __init__(
        self,
        *,
        source_id: int,
        config: BaseModel,
        http: SourceHttpClient,
    ) -> None:
        if not isinstance(config, AshbyConfig):
            raise AdapterConfigError("AshbyAdapter requires an AshbyConfig")
        self.source_id = source_id
        self.config = config
        self._http = http
        #: ``unlisted`` lands here; the runner copies it into
        #: ``SourceResult.skipped`` (§10.3).
        self.skipped: dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def parse_config(cls, raw: dict[str, Any]) -> AshbyConfig:
        """Validate ``source.config`` JSONB into an ``AshbyConfig``.

        Args:
            raw: The stored config object.

        Returns:
            The validated config.

        Raises:
            AdapterConfigError: When the config does not validate.
        """
        try:
            return AshbyConfig.model_validate(raw)
        except ValidationError as exc:
            raise config_error("ashby", exc) from exc

    async def probe(self) -> ProbeResult:
        """Fetch the board once and report reachability.

        Returns:
            A ``ProbeResult`` whose ``sample_count`` counts **listed** postings
            only — an unlisted draft is not evidence that the board is usable.
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
            sample_count=sum(1 for job in page.jobs if job.isListed),
            latency_ms=elapsed_ms(started),
            http_status=HTTP_OK,
            company_name_guess=company_guess(self.config.board_name),
        )

    async def fetch(self, *, since: datetime | None = None) -> AsyncIterator[RawPosting]:
        """Yield every **listed** posting on the board.

        Args:
            since: Ignored. Ashby has no server-side date filter.

        Yields:
            One ``RawPosting`` per listed job with a usable description.

        Raises:
            UpstreamHttpError: On 404 and other 4xx.
            SchemaDriftError: When the response does not match the page model.
        """
        del since
        page = await self._get_board()
        for job in page.jobs:
            if not job.isListed:
                # A draft or a confidential search. Counted, not silent.
                self._count("unlisted")
                continue
            posting = self._to_posting(job)
            if posting is not None:
                yield posting

    async def aclose(self) -> None:
        """Nothing adapter-local to release."""

    def describe(self) -> str:
        """Return the short human string used on the Companies page and digest."""
        return f"Ashby · {self.config.board_name}"

    # -- internals ---------------------------------------------------------

    def _url(self) -> str:
        return f"{self.API_HOST}/posting-api/job-board/{self.config.board_name}"

    async def _get_board(self) -> _AshbyPage:
        params = {"includeCompensation": "true" if self.config.include_compensation else "false"}
        payload = await self._http.get_json(
            self._url(), params=params, url_template=self.URL_TEMPLATE
        )
        try:
            return _AshbyPage.model_validate(payload)
        except ValidationError as exc:
            raise drift_error(self.URL_TEMPLATE, exc) from exc

    def _to_posting(self, job: _AshbyJob) -> RawPosting | None:
        # descriptionPlain is already clean; §9.1 normalisation still runs, so
        # that content_hash does not move when the same text is re-pasted.
        description_text = html_to_text(job.descriptionPlain or job.descriptionHtml)
        if not description_text:
            self._count("no_description")
            log.warning(
                "posting_dropped_no_description",
                source_id=self.source_id,
                adapter=self.name.value,
                external_id=job.id,
            )
            return None

        bounded, truncated = bound_description(
            description_text, self._http.settings.max_description_chars
        )

        parsed = parse_location(job.location)
        secondary_remote = any(
            parse_location(secondary.location).is_remote for secondary in job.secondaryLocations
        )

        raw: dict[str, Any] = {
            "id": job.id,
            "department": job.department,
            "team": job.team,
            "employmentType": job.employmentType,
            "isRemote": job.isRemote,
            "secondaryLocations": [s.model_dump() for s in job.secondaryLocations],
            # Kept because it is the employer's own public statement, and shown
            # on the posting page. Never an input to scoring.
            "compensation": job.compensation,
        }
        if truncated:
            raw["description_truncated"] = True

        return RawPosting(
            external_id=job.id,
            url=job.jobUrl,
            title=job.title,
            description_html=job.descriptionHtml,
            description_text=bounded,
            department=job.team or job.department,
            location_raw=job.location,
            location_city=parsed.city,
            location_country=parsed.country,
            is_remote=job.isRemote or parsed.is_remote or secondary_remote,
            employment_type=map_employment_type(job.employmentType),
            seniority_guess=infer_seniority(job.title),
            posted_at=parse_iso_datetime(job.publishedAt),
            raw=raw,
        )

    def _count(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


__all__ = ["AshbyAdapter"]
