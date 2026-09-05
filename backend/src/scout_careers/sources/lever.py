"""Lever postings adapter (SOURCE_ADAPTERS.md §5.2).

Fidelity 88 — complete once ``lists`` are assembled, and the assembly is a
reconstruction step, which is the notch below Greenhouse and Ashby.

The caveats from §5.2, in the order they bite:

- **The response is a bare JSON array**, not an object with a ``jobs`` key.
  Code that assumes an envelope breaks here and only here, so the page model is
  a ``RootModel`` over a list.
- **The requirements live in ``lists``, not in ``description``.** An adapter
  that maps only ``descriptionPlain`` throws away exactly the text stage ⑤
  extracts requirements from, and every Lever posting then scores near zero.
  This is the highest-value line in the whole subsection: ``description_html``
  is ``description`` + each ``lists[].text`` as an ``<h3>`` + ``lists[].content``
  + ``additional``, and ``description_text`` is the same assembly in plain form.
- ``createdAt`` is **epoch milliseconds**. Read as seconds it dates every
  posting to 1970 and recency ranking silently inverts, so it goes through
  ``parse_epoch_millis`` and its 2000–2100 sanity window.
- ``allLocations`` may hold several cities for one posting. The first is kept as
  canonical, the full list goes to ``raw["all_locations"]``, and one posting is
  never fanned out into several — one Lever posting is one application.

**Pagination.** One request by default; ``?limit=`` / ``?skip=`` exist and are
used only when a limit was explicitly supplied. The loop is therefore dormant in
production and engages only for an operator who set one. It terminates three
ways: a short page ends it, an empty page ends it, and ``MAX_PAGES`` bounds it
regardless of what the upstream returns.
"""

from __future__ import annotations

import asyncio
import html as html_entities
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from scout_careers.common.errors import AdapterConfigError, ScoutError, UpstreamHttpError
from scout_careers.common.logging import get_logger
from scout_careers.common.types import AtsType
from scout_careers.sources._shared import (
    NullIsEmptyList,
    NullIsEmptyModel,
    bound_description,
    company_guess,
    config_error,
    drift_error,
    elapsed_ms,
    probe_detail,
)
from scout_careers.sources.base import LeverConfig, ProbeResult, RawPosting
from scout_careers.sources.http import SourceHttpClient
from scout_careers.sources.normalise import (
    html_to_text,
    infer_seniority,
    map_employment_type,
    parse_epoch_millis,
    parse_location,
)

log = get_logger(__name__)

HTTP_OK = 200

#: Lever's own value for a fully remote posting.
REMOTE_WORKPLACE_TYPE = "remote"


# ---------------------------------------------------------------------------
# The upstream page shape (§12 step 5)
# ---------------------------------------------------------------------------


class _LeverCategories(BaseModel):
    model_config = ConfigDict(extra="ignore")

    commitment: str | None = None
    department: str | None = None
    team: str | None = None
    location: str | None = None
    # Defaulted fields tolerate an explicit null (_shared.py).
    allLocations: Annotated[  # noqa: N815 - upstream spelling
        list[str], NullIsEmptyList
    ] = Field(default_factory=list)


class _LeverList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str | None = None
    content: str | None = None


class _LeverPosting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Annotated[str, Field(min_length=1)]
    text: Annotated[str, Field(min_length=1)]
    hostedUrl: str  # noqa: N815 - upstream spelling
    applyUrl: str | None = None  # noqa: N815 - upstream spelling
    createdAt: int | float | None = None  # noqa: N815 - upstream spelling
    workplaceType: str | None = None  # noqa: N815 - upstream spelling
    categories: Annotated[_LeverCategories, NullIsEmptyModel] = Field(
        default_factory=_LeverCategories
    )
    description: str | None = None
    descriptionPlain: str | None = None  # noqa: N815 - upstream spelling
    lists: Annotated[list[_LeverList], NullIsEmptyList] = Field(default_factory=list)
    additional: str | None = None
    additionalPlain: str | None = None  # noqa: N815 - upstream spelling


class _LeverPage(RootModel[list[_LeverPosting]]):
    """A bare JSON array. There is no envelope, and that is the point."""

    root: list[_LeverPosting]


class LeverAdapter:
    """Reads one Lever site, normally in a single request."""

    name: ClassVar[AtsType] = AtsType.LEVER
    config_model: ClassVar[type[BaseModel]] = LeverConfig
    fidelity_rank: ClassVar[int] = 88
    default_poll_interval_minutes: ClassVar[int] = 1440
    requires_detail_fetch: ClassVar[bool] = False

    API_HOST: ClassVar[str] = "https://api.lever.co"
    URL_TEMPLATE: ClassVar[str] = "api.lever.co/v0/postings/{site}"

    #: Hard bound on the dormant paging loop. It exists so that an upstream that
    #: keeps returning full pages — a bug, or a cursor we misuse — cannot spin
    #: this adapter forever inside the per-source ceiling.
    MAX_PAGES: ClassVar[int] = 20

    def __init__(
        self,
        *,
        source_id: int,
        config: BaseModel,
        http: SourceHttpClient,
        limit: int | None = None,
    ) -> None:
        if not isinstance(config, LeverConfig):
            raise AdapterConfigError("LeverAdapter requires a LeverConfig")
        self.source_id = source_id
        self.config = config
        self._http = http
        #: Page size. ``None`` — the default, and what the runner constructs —
        #: means one unpaged request, which is what every observed board needs.
        #: A value here is what wakes the paging loop up.
        self.limit = limit
        self.skipped: dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def parse_config(cls, raw: dict[str, Any]) -> LeverConfig:
        """Validate ``source.config`` JSONB into a ``LeverConfig``.

        Args:
            raw: The stored config object.

        Returns:
            The validated config.

        Raises:
            AdapterConfigError: When the config does not validate.
        """
        try:
            return LeverConfig.model_validate(raw)
        except ValidationError as exc:
            raise config_error("lever", exc) from exc

    async def probe(self) -> ProbeResult:
        """Fetch the site once — never paged — and report reachability.

        Returns:
            A ``ProbeResult`` with a curated ``detail``.
        """
        started = time.monotonic()
        try:
            page = await asyncio.wait_for(
                self._get_page(limit=None, skip=None),
                timeout=self._http.settings.source_probe_timeout_s,
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
            sample_count=len(page.root),
            latency_ms=elapsed_ms(started),
            http_status=HTTP_OK,
            company_name_guess=company_guess(self.config.site),
        )

    async def fetch(self, *, since: datetime | None = None) -> AsyncIterator[RawPosting]:
        """Yield every currently-listed posting for this site.

        Args:
            since: Ignored. Lever has no server-side date filter.

        Yields:
            One ``RawPosting`` per posting with a usable description.

        Raises:
            UpstreamHttpError: On 404 and other 4xx.
            SchemaDriftError: When the array does not match the posting model.
        """
        del since
        if self.limit is None:
            page = await self._get_page(limit=None, skip=None)
            for posting in page.root:
                mapped = self._to_posting(posting)
                if mapped is not None:
                    yield mapped
            return

        # Dormant loop. Only an explicitly configured page size gets here.
        seen: set[str] = set()
        skip = 0
        for _page_index in range(self.MAX_PAGES):
            page = await self._get_page(limit=self.limit, skip=skip)
            batch = page.root
            if not batch:
                return
            for posting in batch:
                if posting.id in seen:
                    # An upstream that repeats a page would otherwise loop
                    # forever inside MAX_PAGES emitting duplicates.
                    self._count("duplicate")
                    continue
                seen.add(posting.id)
                mapped = self._to_posting(posting)
                if mapped is not None:
                    yield mapped
            if len(batch) < self.limit:
                # A short page is the end of the board.
                return
            skip += self.limit

        log.warning(
            "lever_pagination_capped",
            source_id=self.source_id,
            adapter=self.name.value,
            url_template=self.URL_TEMPLATE,
            max_pages=self.MAX_PAGES,
        )

    async def aclose(self) -> None:
        """Nothing adapter-local to release."""

    def describe(self) -> str:
        """Return the short human string used on the Companies page and digest."""
        return f"Lever · {self.config.site}"

    # -- internals ---------------------------------------------------------

    def _url(self) -> str:
        return f"{self.API_HOST}/v0/postings/{self.config.site}"

    async def _get_page(self, *, limit: int | None, skip: int | None) -> _LeverPage:
        params: dict[str, Any] = {"mode": "json"}
        if limit is not None:
            params["limit"] = limit
        if skip:
            params["skip"] = skip
        payload = await self._http.get_json(
            self._url(), params=params, url_template=self.URL_TEMPLATE
        )
        try:
            return _LeverPage.model_validate(payload)
        except ValidationError as exc:
            raise drift_error(self.URL_TEMPLATE, exc) from exc

    def _to_posting(self, posting: _LeverPosting) -> RawPosting | None:
        description_html = _assemble_html(posting)
        description_text = _assemble_text(posting)
        if not description_text:
            self._count("no_description")
            log.warning(
                "posting_dropped_no_description",
                source_id=self.source_id,
                adapter=self.name.value,
                external_id=posting.id,
            )
            return None

        bounded, truncated = bound_description(
            description_text, self._http.settings.max_description_chars
        )

        categories = posting.categories
        # The first of allLocations is canonical; the rest are provenance. One
        # Lever posting is one application, so it is never fanned out.
        all_locations = list(categories.allLocations)
        location_raw = categories.location or (all_locations[0] if all_locations else None)
        parsed = parse_location(location_raw)
        workplace_remote = (posting.workplaceType or "").strip().lower() == REMOTE_WORKPLACE_TYPE

        raw: dict[str, Any] = {
            "id": posting.id,
            "categories": categories.model_dump(),
            "workplaceType": posting.workplaceType,
            "createdAt": posting.createdAt,
            "all_locations": all_locations,
        }
        if truncated:
            raw["description_truncated"] = True

        return RawPosting(
            external_id=posting.id,
            url=posting.hostedUrl,
            title=posting.text,
            description_html=description_html,
            description_text=bounded,
            department=categories.team or categories.department,
            location_raw=location_raw,
            location_city=parsed.city,
            location_country=parsed.country,
            is_remote=workplace_remote or parsed.is_remote,
            employment_type=map_employment_type(categories.commitment),
            seniority_guess=infer_seniority(posting.text),
            # Epoch MILLISECONDS. Seconds would date the board to 1970.
            posted_at=parse_epoch_millis(posting.createdAt),
            raw=raw,
        )

    def _count(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _assemble_html(posting: _LeverPosting) -> str | None:
    """Reassemble the full description HTML from Lever's split fields.

    Args:
        posting: One validated posting.

    Returns:
        ``description`` + each ``lists`` block rendered as an ``<h3>`` heading
        followed by its ``content`` + ``additional``, or ``None`` when the
        posting carries no HTML at all.
    """
    parts: list[str] = []
    if posting.description:
        parts.append(posting.description)
    for block in posting.lists:
        if block.text:
            # Escaped: a section heading is upstream text, and it is being
            # placed into markup we assemble.
            parts.append(f"<h3>{html_entities.escape(block.text)}</h3>")
        if block.content:
            parts.append(f"<ul>{block.content}</ul>")
    if posting.additional:
        parts.append(posting.additional)
    return "\n".join(parts) if parts else None


def _assemble_text(posting: _LeverPosting) -> str:
    """Reassemble the plain description, requirements included.

    The lists are flattened through ``html_to_text``, which preserves ``<li>``
    structure as bullets — that structure is what stage ⑤ reads requirements
    from, and losing it is the failure this adapter exists to avoid.

    Args:
        posting: One validated posting.

    Returns:
        The assembled plain text, possibly empty.
    """
    parts: list[str] = []
    intro = posting.descriptionPlain or html_to_text(posting.description)
    if intro.strip():
        parts.append(intro.strip())
    for block in posting.lists:
        section: list[str] = []
        if block.text:
            section.append(block.text.strip())
        flattened = html_to_text(block.content)
        if flattened:
            section.append(flattened)
        if section:
            parts.append("\n".join(section))
    closing = posting.additionalPlain or html_to_text(posting.additional)
    if closing.strip():
        parts.append(closing.strip())
    return "\n\n".join(parts).strip()


__all__ = ["REMOTE_WORKPLACE_TYPE", "LeverAdapter"]
