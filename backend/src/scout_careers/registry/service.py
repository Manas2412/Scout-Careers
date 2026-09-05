"""Company and source CRUD, and ATS auto-detection.

Detection is three steps in a fixed order, and the order is the design
(COMPANY_REGISTRY.md §2.1):

1. **Policy gate.** ``assert_fetch_allowed`` on the pasted host, before the HTTP
   client is touched, so a never-fetch host is refused without the refusal being
   observable to it. There is no argument that turns this off.
2. **Pattern match** to ``(adapter, config)``. A pattern that captures a value
   the adapter's config model rejects is treated as a non-match, not an error:
   the pattern table proposes and the config model disposes.
3. **One live probe.** Detection never returns a config it has not probed. A
   pattern match is a guess about a URL shape; a probe is evidence that the
   endpoint exists and returns postings. Writing an unprobed source is how a
   registry accumulates forty quietly-dead rows nobody notices for a month.

Detection is read-only. It creates nothing — the operator confirms, and
:func:`create_company` / :func:`create_source` do the writing.

The canonical config serialisation matters more than it looks. ``source`` is
``UNIQUE (company_id, adapter, config)`` (DATA_MODEL.md §3.2), and that
uniqueness only works if the same board always serialises the same way. So
configs are written as ``model_dump(mode="json", exclude_defaults=False)`` with
sorted keys: defaults are materialised rather than omitted, so a config saved
before a default changed still collides with one saved after.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scout_careers.common.clock import utcnow
from scout_careers.common.config import Settings, get_settings
from scout_careers.common.errors import ScoutError
from scout_careers.common.logging import get_logger
from scout_careers.common.types import AtsType, CompanyStatus, CompanyTier
from scout_careers.db.models import Company, Source
from scout_careers.sources.base import AshbyConfig, GreenhouseConfig, LeverConfig, ProbeResult
from scout_careers.sources.http import (
    STATIC_BUCKET_KEYS,
    InRunCircuitBreaker,
    NullRateLimiter,
    RateLimiter,
    RedisTokenBucket,
    RobotsPolicy,
    SourceHttpClient,
    build_client,
)
from scout_careers.sources.policy import assert_fetch_allowed
from scout_careers.sources.registry import get_adapter

log = get_logger(__name__)

#: The axes ``company.tags`` accepts. Values are open, axes are not: axes are
#: structure, values are vocabulary, and an untyped tag becomes a folksonomy
#: within a month (COMPANY_REGISTRY.md §5.1).
TAG_AXES: Final[frozenset[str]] = frozenset({"sector", "stage", "geo", "role", "origin", "watch"})

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class RegistryError(ScoutError):
    """A registry write was refused."""

    error_code: ClassVar[str] = "company.invalid"


class TagInvalid(RegistryError):
    """A tag is missing its axis prefix, or uses an axis that does not exist."""

    error_code: ClassVar[str] = "company.tag_invalid"


class Undetectable(ScoutError):
    """No supported ATS was found at the URL."""

    error_code: ClassVar[str] = "source.undetectable"


class DuplicateSource(RegistryError):
    """This company already has a source with this adapter and config."""

    error_code: ClassVar[str] = "source.duplicate"


# ---------------------------------------------------------------------------
# Canonical values
# ---------------------------------------------------------------------------


def slugify(name: str) -> str:
    """Return the registry slug for a company name.

    Args:
        name: The display name.

    Returns:
        A lower-case, hyphenated ASCII slug. Accents are folded rather than
        dropped, so "Société" and "Societe" do not become two companies.
    """
    folded = unicodedata.normalize("NFKD", name)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii").lower()
    return _SLUG_STRIP.sub("-", ascii_only).strip("-") or "company"


def canonical_config(config: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    """Serialise an adapter config canonically for storage and comparison.

    Args:
        config: A validated config model, or an already-plain mapping.

    Returns:
        A JSON-safe dict with sorted keys and every default materialised.
        ``exclude_defaults=False`` is deliberate: omitting a defaulted field
        would make two identical boards compare unequal the day the default
        changes, and ``UNIQUE (company_id, adapter, config)`` would stop
        catching the duplicate.
    """
    raw = (
        config.model_dump(mode="json", exclude_defaults=False)
        if isinstance(config, BaseModel)
        else json.loads(json.dumps(dict(config), default=str))
    )
    return {key: raw[key] for key in sorted(raw)}


def validate_tags(tags: Sequence[str]) -> list[str]:
    """Check and canonicalise company tags.

    Args:
        tags: Tags in ``axis:value`` form.

    Returns:
        The tags, lower-cased, de-duplicated and sorted.

    Raises:
        TagInvalid: When a tag has no axis, or an axis outside :data:`TAG_AXES`.
    """
    cleaned: set[str] = set()
    for tag in tags:
        candidate = tag.strip().lower()
        if not candidate:
            continue
        axis, separator, value = candidate.partition(":")
        if not separator or not value:
            raise TagInvalid(f"tag {candidate!r} must be in axis:value form")
        if axis not in TAG_AXES:
            raise TagInvalid(
                f"unknown tag axis {axis!r}; known axes: {', '.join(sorted(TAG_AXES))}"
            )
        cleaned.add(candidate)
    return sorted(cleaned)


# ---------------------------------------------------------------------------
# §2.2 The URL pattern table (Phase 1 adapters)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PatternMatch:
    """One ``(adapter, config)`` proposal from the pattern table."""

    adapter: AtsType
    config: BaseModel

    @property
    def config_json(self) -> dict[str, Any]:
        """Return the canonical stored form of this config."""
        return canonical_config(self.config)


@dataclass(frozen=True, slots=True)
class _Pattern:
    """A host/path shape and the adapter it implies."""

    regex: re.Pattern[str]
    adapter: AtsType
    field: str


#: Tried in order. Phase 1 detects the three multi-tenant ATSs it has adapters
#: for; the Workday, SmartRecruiters, Workable and Recruitee rows of §2.2 arrive
#: with their adapters, because detecting a board this build cannot fetch would
#: create a source that fails every night.
PATTERNS: Final[tuple[_Pattern, ...]] = (
    _Pattern(
        re.compile(r"^boards\.greenhouse\.io/(?P<value>[^/]+)/?$"),
        AtsType.GREENHOUSE,
        "board_token",
    ),
    _Pattern(
        re.compile(r"^job-boards\.greenhouse\.io/(?P<value>[^/]+)/?"),
        AtsType.GREENHOUSE,
        "board_token",
    ),
    _Pattern(
        re.compile(r"^boards-api\.greenhouse\.io/v1/boards/(?P<value>[^/]+)"),
        AtsType.GREENHOUSE,
        "board_token",
    ),
    _Pattern(
        re.compile(r"^jobs\.lever\.co/(?P<value>[^/]+)/?"),
        AtsType.LEVER,
        "site",
    ),
    _Pattern(
        re.compile(r"^api\.lever\.co/v0/postings/(?P<value>[^/]+)"),
        AtsType.LEVER,
        "site",
    ),
    _Pattern(
        re.compile(r"^jobs\.ashbyhq\.com/(?P<value>[^/]+)/?"),
        AtsType.ASHBY,
        "board_name",
    ),
    _Pattern(
        re.compile(r"^api\.ashbyhq\.com/posting-api/job-board/(?P<value>[^/]+)"),
        AtsType.ASHBY,
        "board_name",
    ),
)

#: Config model per adapter, so a captured value is validated by the same model
#: the runner will validate it with.
CONFIG_MODELS: Final[Mapping[AtsType, type[BaseModel]]] = {
    AtsType.GREENHOUSE: GreenhouseConfig,
    AtsType.LEVER: LeverConfig,
    AtsType.ASHBY: AshbyConfig,
}


def normalise_url(url: str) -> tuple[str, str | None]:
    """Reduce a pasted URL to the string the pattern table matches against.

    Args:
        url: Whatever the operator pasted.

    Returns:
        ``(host_and_path, embed_token)``. The host is lower-cased, the query and
        fragment are dropped and a trailing slash is removed. ``embed_token`` is
        the ``for=`` parameter of a legacy Greenhouse embed URL, which is the one
        case where the query string carries the identity.
    """
    parsed = httpx.URL(url if "://" in url else f"https://{url}")
    host = (parsed.host or "").lower()
    embed_token: str | None = None
    if host.endswith("greenhouse.io") and parsed.path.startswith("/embed/job_board"):
        value = parsed.params.get("for")
        embed_token = str(value) if value else None
    path = parsed.path.rstrip("/")
    return f"{host}{path}", embed_token


def match_url(url: str) -> PatternMatch | None:
    """Match a URL against the pattern table.

    Args:
        url: An already policy-checked URL.

    Returns:
        The first match whose captured value validates against the adapter's
        config model, or ``None``. A capture the model rejects is a non-match,
        so a URL like ``jobs.lever.co/Some%20Page`` falls through to the next
        pattern rather than producing a config that would fail at run time.
    """
    target, embed_token = normalise_url(url)

    if embed_token is not None:
        built = _build_config(AtsType.GREENHOUSE, "board_token", embed_token)
        if built is not None:
            return PatternMatch(adapter=AtsType.GREENHOUSE, config=built)

    for pattern in PATTERNS:
        found = pattern.regex.match(target)
        if found is None:
            continue
        built = _build_config(pattern.adapter, pattern.field, found.group("value"))
        if built is not None:
            return PatternMatch(adapter=pattern.adapter, config=built)
    return None


def _build_config(adapter: AtsType, field: str, value: str) -> BaseModel | None:
    """Validate one captured value into the adapter's config model."""
    model = CONFIG_MODELS[adapter]
    try:
        return model.model_validate({field: value})
    except ValidationError:
        return None


# ---------------------------------------------------------------------------
# §2.3 Detection
# ---------------------------------------------------------------------------


class DetectionResult(BaseModel):
    """What detection found, and the evidence for it."""

    model_config = ConfigDict(extra="forbid")

    adapter: AtsType
    config: dict[str, Any]
    company_name_guess: str | None = None
    probe: ProbeResult
    existing_company_id: int | None = None
    existing_source_id: int | None = None

    @property
    def usable(self) -> bool:
        """Report whether the probe actually reached a board."""
        return self.probe.reachable


async def detect_ats(
    url: str,
    *,
    settings: Settings,
    client: httpx.AsyncClient,
    limiter: RateLimiter,
    robots: RobotsPolicy,
    breaker: InRunCircuitBreaker | None = None,
) -> DetectionResult:
    """Detect the ATS behind a careers URL and prove it with one probe.

    Args:
        url: The pasted URL.
        settings: Configuration.
        client: A shared httpx client.
        limiter: The rate limiter the probe leases from.
        robots: The robots policy the probe honours.
        breaker: An in-run circuit breaker; a fresh one when omitted.

    Returns:
        The detection result, always including a probe.

    Raises:
        DeniedByPolicy: The host is on the never-fetch list. No request is made,
            and this is not overridable.
        Undetectable: No pattern matched.
    """
    assert_fetch_allowed(url)

    match = match_url(url)
    if match is None:
        raise Undetectable(
            "no supported ATS found at that URL; paste the board URL itself, "
            "or add the company with no source"
        )

    adapter_cls = get_adapter(match.adapter)
    http = SourceHttpClient(
        client,
        settings=settings,
        source_id=0,
        adapter=match.adapter,
        bucket_key=STATIC_BUCKET_KEYS[match.adapter],
        limiter=limiter,
        robots=robots,
        breaker=breaker or InRunCircuitBreaker(threshold=settings.circuit_breaker_failures),
    )
    adapter = adapter_cls(source_id=0, config=match.config, http=http)
    try:
        probe = await adapter.probe()
    finally:
        await adapter.aclose()

    return DetectionResult(
        adapter=match.adapter,
        config=match.config_json,
        company_name_guess=probe.company_name_guess,
        probe=probe,
    )


async def detect_for_url(
    url: str,
    *,
    settings: Settings | None = None,
    redis: Redis | None = None,
) -> DetectionResult:
    """Detect the ATS behind a URL, building and closing the HTTP client.

    Args:
        url: The pasted URL.
        settings: Configuration; resolved from the environment when omitted.
        redis: Redis, for the shared token bucket and the robots cache. Without
            it the probe still honours robots.txt and still refuses never-fetch
            hosts; it simply does not share a rate-limit bucket with a
            concurrent run. That is acceptable for exactly one request, and
            nothing else in this system is allowed to make the same trade.

    Returns:
        The detection result.
    """
    resolved = settings or get_settings()
    client = build_client(resolved)
    try:
        robots = RobotsPolicy(
            client,
            user_agent=resolved.source_user_agent,
            cache_ttl_s=resolved.robots_cache_ttl_s,
            timeout_s=resolved.source_probe_timeout_s,
            redis=redis,
        )
        limiter: RateLimiter = RedisTokenBucket(redis) if redis is not None else NullRateLimiter()
        return await detect_ats(
            url, settings=resolved, client=client, limiter=limiter, robots=robots
        )
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


async def get_company(session: AsyncSession, company_id: int) -> Company | None:
    """Return one company by id, or ``None``."""
    return await session.get(Company, company_id)


async def get_company_by_slug(session: AsyncSession, slug: str) -> Company | None:
    """Return one company by slug, or ``None``."""
    result = await session.execute(select(Company).where(Company.slug == slug))
    return result.scalar_one_or_none()


async def list_companies(
    session: AsyncSession,
    *,
    status: CompanyStatus | None = None,
    tier: CompanyTier | None = None,
    include_deleted: bool = False,
) -> list[Company]:
    """List companies for the Companies page and the CLI.

    Args:
        session: An open session.
        status: Restrict to one status.
        tier: Restrict to one tier.
        include_deleted: Include soft-deleted rows.

    Returns:
        Companies ordered by name.
    """
    stmt = select(Company).order_by(Company.name)
    if not include_deleted:
        stmt = stmt.where(Company.deleted_at.is_(None))
    if status is not None:
        stmt = stmt.where(Company.status == status)
    if tier is not None:
        stmt = stmt.where(Company.tier == tier)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_company(
    session: AsyncSession,
    *,
    name: str,
    slug: str | None = None,
    tier: CompanyTier = CompanyTier.VOLUME,
    tags: Sequence[str] = (),
    careers_url: str | None = None,
    website: str | None = None,
    hq_location: str | None = None,
    notes: str | None = None,
) -> Company:
    """Create a company, or return the existing row with that slug.

    Idempotent on ``slug`` so that re-running the seed updates nothing the
    operator has since edited (COMPANY_REGISTRY.md §9).

    Args:
        session: An open session. The caller owns the transaction.
        name: Display name.
        slug: Registry slug; derived from ``name`` when omitted.
        tier: ``dream`` / ``strong`` / ``volume``.
        tags: ``axis:value`` tags.
        careers_url: The careers page. Stored even when it is a never-fetch
            host — storing a link and following one are separate acts, and only
            the second is forbidden (COMPANY_REGISTRY.md §2.4).
        website: Company website.
        hq_location: Free text.
        notes: Free text.

    Returns:
        The company row, existing or new.

    Raises:
        TagInvalid: When a tag is malformed.
    """
    resolved_slug = slug or slugify(name)
    existing = await get_company_by_slug(session, resolved_slug)
    if existing is not None:
        return existing

    company = Company(
        slug=resolved_slug,
        name=name,
        tier=tier,
        status=CompanyStatus.TRACKING,
        tags=validate_tags(tags),
        careers_url=careers_url,
        website=website,
        hq_location=hq_location,
        notes=notes,
    )
    session.add(company)
    await session.flush()
    return company


async def set_company_status(
    session: AsyncSession,
    company_id: int,
    status: CompanyStatus,
    *,
    cascade_sources: bool = True,
) -> Company:
    """Pause, resume or blacklist a company.

    Args:
        session: An open session.
        company_id: The company.
        status: The new status.
        cascade_sources: Also switch the company's sources. Pausing a company
            whose sources keep polling would be a setting that does nothing,
            and re-enabling is the documented human act that also clears
            ``consecutive_failures`` (SOURCE_ADAPTERS.md §4.8).

    Returns:
        The updated company.

    Raises:
        RegistryError: When the company does not exist.
    """
    company = await session.get(Company, company_id)
    if company is None:
        raise RegistryError(f"company {company_id} does not exist")
    company.status = status

    if cascade_sources:
        enabled = status is CompanyStatus.TRACKING
        values: dict[str, Any] = {"enabled": enabled}
        if enabled:
            values.update(consecutive_failures=0, last_error=None)
        await session.execute(
            update(Source).where(Source.company_id == company_id).values(**values)
        )
    await session.flush()
    return company


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


async def list_sources(session: AsyncSession, *, company_id: int | None = None) -> list[Source]:
    """List sources, optionally for one company, ordered by id."""
    stmt = select(Source).order_by(Source.id)
    if company_id is not None:
        stmt = stmt.where(Source.company_id == company_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def find_source(
    session: AsyncSession,
    *,
    company_id: int,
    adapter: AtsType,
    config: Mapping[str, Any],
) -> Source | None:
    """Find a company's source by adapter and canonical config.

    Args:
        session: An open session.
        company_id: The owning company.
        adapter: The adapter type.
        config: The config to compare, canonicalised before comparison.

    Returns:
        The matching source, or ``None``.
    """
    wanted = canonical_config(config)
    result = await session.execute(
        select(Source).where(Source.company_id == company_id, Source.adapter == adapter)
    )
    for source in result.scalars().all():
        if canonical_config(source.config) == wanted:
            return source
    return None


async def create_source(
    session: AsyncSession,
    *,
    company_id: int,
    adapter: AtsType,
    config: BaseModel | Mapping[str, Any],
    poll_interval_minutes: int | None = None,
    enabled: bool = True,
) -> Source:
    """Attach a board to a company.

    Args:
        session: An open session.
        company_id: The owning company.
        adapter: The adapter type.
        config: A validated config model, or a mapping that will be validated.
        poll_interval_minutes: Overrides the adapter's default.
        enabled: Whether the source polls.

    Returns:
        The source row; the existing one when this board is already attached,
        which is what makes seeding idempotent.

    Raises:
        AdapterConfigError: When the config does not validate for this adapter.
    """
    adapter_cls = get_adapter(adapter)
    validated = config if isinstance(config, BaseModel) else adapter_cls.parse_config(dict(config))
    stored = canonical_config(validated)

    existing = await find_source(session, company_id=company_id, adapter=adapter, config=stored)
    if existing is not None:
        return existing

    source = Source(
        company_id=company_id,
        adapter=adapter,
        config=stored,
        enabled=enabled,
        poll_interval_minutes=(
            poll_interval_minutes
            if poll_interval_minutes is not None
            else adapter_cls.default_poll_interval_minutes
        ),
    )
    session.add(source)
    await session.flush()
    return source


async def set_source_enabled(session: AsyncSession, source_id: int, enabled: bool) -> Source:
    """Enable or disable one source.

    Enabling is the human act that also resets ``consecutive_failures``: the
    system never re-enables itself on a timer, because five consecutive daily
    failures almost always means the board moved and needs a new config
    (SOURCE_ADAPTERS.md §4.8).

    Args:
        session: An open session.
        source_id: The source.
        enabled: The new state.

    Returns:
        The updated source.

    Raises:
        RegistryError: When the source does not exist.
    """
    source = await session.get(Source, source_id)
    if source is None:
        raise RegistryError(f"source {source_id} does not exist")
    source.enabled = enabled
    if enabled:
        source.consecutive_failures = 0
        source.last_error = None
    else:
        source.last_status = "disabled"
    await session.flush()
    return source


async def touch_company_seen(session: AsyncSession, company_id: int) -> None:
    """Record that a company was reviewed, without changing anything else."""
    await session.execute(
        update(Company).where(Company.id == company_id).values(updated_at=utcnow())
    )


__all__ = [
    "CONFIG_MODELS",
    "PATTERNS",
    "TAG_AXES",
    "DetectionResult",
    "DuplicateSource",
    "PatternMatch",
    "RegistryError",
    "TagInvalid",
    "Undetectable",
    "canonical_config",
    "create_company",
    "create_source",
    "detect_ats",
    "detect_for_url",
    "find_source",
    "get_company",
    "get_company_by_slug",
    "list_companies",
    "list_sources",
    "match_url",
    "normalise_url",
    "set_company_status",
    "set_source_enabled",
    "slugify",
    "touch_company_seen",
    "validate_tags",
]
