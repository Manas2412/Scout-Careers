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

That uniqueness is also why this module owns *retiring* a source. Changing a
board token in ``seeds/companies.yaml`` and re-seeding does not edit the old row;
it inserts a second one, and the dead one keeps failing nightly until the
five-failure threshold disables it. :func:`retire_source` is the intended
remedy and :func:`delete_source` is deliberately not — deleting a source
cascades to every posting ever discovered through it (DATA_MODEL.md §4.1), which
is history nothing can reconstruct.
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
from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scout_careers.common.clock import utcnow
from scout_careers.common.config import Settings, get_settings
from scout_careers.common.errors import AdapterConfigError, ScoutError
from scout_careers.common.logging import REDACT_KEY_RE, REDACTED, get_logger
from scout_careers.common.types import AtsType, CompanyStatus, CompanyTier
from scout_careers.db.models import Company, JobPosting, Source
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
    build_source_client,
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


class SourceNotFound(RegistryError):
    """No source with that id."""

    error_code: ClassVar[str] = "source.not_found"


class SourceNotProbeable(RegistryError):
    """This source has no board URL, so there is nothing to re-probe.

    ``mail_alert`` is the Phase 1 case: it reads the alerts mailbox and issues no
    HTTP request at all. Raised rather than probed, because a probe whose target
    URL cannot be derived is a probe the never-scrape gate cannot check, and an
    unchecked fetch is the one thing this module will not do.
    """

    error_code: ClassVar[str] = "source.not_probeable"


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


def sources_query(
    *,
    company_id: int | None = None,
    adapter: AtsType | None = None,
    last_status: str | None = None,
    failing: bool = False,
    enabled: bool | None = None,
) -> Select[tuple[Source]]:
    """Build the filtered ``source`` select the list views read.

    Split out from :func:`list_sources` so the filter itself is testable without
    a database: the statement is the thing that can be wrong, and compiling it is
    cheaper than standing up Postgres to find out.

    Args:
        company_id: Restrict to one company.
        adapter: Restrict to one adapter type.
        last_status: Restrict to one ``source.last_status`` value. Free text
            rather than an enum because the status vocabulary lives in a TEXT
            column and grows without a migration (``common/types.py``).
        failing: Only sources with at least one consecutive failure.
        enabled: Only enabled, or only disabled, sources.

    Returns:
        A ``Select`` over :class:`~scout_careers.db.models.Source`, ordered by id.
    """
    stmt = select(Source).order_by(Source.id)
    if company_id is not None:
        stmt = stmt.where(Source.company_id == company_id)
    if adapter is not None:
        stmt = stmt.where(Source.adapter == adapter)
    if last_status is not None:
        stmt = stmt.where(Source.last_status == last_status)
    if failing:
        stmt = stmt.where(Source.consecutive_failures > 0)
    if enabled is not None:
        stmt = stmt.where(Source.enabled.is_(enabled))
    return stmt


async def list_sources(
    session: AsyncSession,
    *,
    company_id: int | None = None,
    adapter: AtsType | None = None,
    last_status: str | None = None,
    failing: bool = False,
    enabled: bool | None = None,
) -> list[Source]:
    """List sources, filtered as :func:`sources_query` describes, ordered by id."""
    result = await session.execute(
        sources_query(
            company_id=company_id,
            adapter=adapter,
            last_status=last_status,
            failing=failing,
            enabled=enabled,
        )
    )
    return list(result.scalars().all())


async def get_source(session: AsyncSession, source_id: int) -> Source | None:
    """Return one source by id, or ``None``."""
    return await session.get(Source, source_id)


async def require_source(session: AsyncSession, source_id: int) -> Source:
    """Return one source by id.

    Args:
        session: An open session.
        source_id: The source.

    Returns:
        The source row.

    Raises:
        SourceNotFound: When there is no such source.
    """
    source = await session.get(Source, source_id)
    if source is None:
        raise SourceNotFound(f"source {source_id} does not exist")
    return source


async def resolve_company(session: AsyncSession, reference: str) -> Company:
    """Resolve a company by slug, or by id when the reference is all digits.

    Args:
        session: An open session.
        reference: A slug (``anysphere``) or an id (``42``).

    Returns:
        The company.

    Raises:
        RegistryError: When nothing matches.
    """
    company = (
        await get_company(session, int(reference))
        if reference.isdigit()
        else await get_company_by_slug(session, reference)
    )
    if company is None:
        raise RegistryError(f"no company {reference!r}; try `scout-careers company list`")
    return company


async def companies_by_id(session: AsyncSession, ids: Sequence[int]) -> dict[int, Company]:
    """Load the companies named by ``ids``, keyed by id.

    Soft-deleted companies are included: their sources still exist and still
    appear in a source listing, and hiding the owner's name would make the row
    unreadable rather than tidy.
    """
    if not ids:
        return {}
    result = await session.execute(select(Company).where(Company.id.in_(sorted(set(ids)))))
    return {company.id: company for company in result.scalars().all()}


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


# ---------------------------------------------------------------------------
# Retiring, deleting and re-probing a source (COMPANY_REGISTRY.md §11.1–§11.2)
# ---------------------------------------------------------------------------


#: ``source.last_status`` for a board the operator retired by hand. Distinct
#: from ``disabled`` (paused, expected back) and from ``auto_disabled`` (the
#: five-failure threshold fired): ``retired`` says a human looked at it and
#: decided this board is gone for good, which is the state a moved board ends up
#: in and the state the digest should stop nagging about.
RETIRED_STATUS: Final[str] = "retired"

#: Config keys that name the board rather than describe how to read it. These
#: are public identifiers — the token in the URL an employer publishes — so they
#: are shown in full even when :data:`REDACT_KEY_RE` matches the key name, which
#: it does for ``board_token``.
IDENTITY_KEYS: Final[tuple[str, ...]] = (
    "board_token",
    "site",
    "board_name",
    "company_id",
    "account",
    "company",
    "host",
    "tenant",
    "label",
)


def config_identity(config: Mapping[str, Any]) -> str | None:
    """Return the one config value that identifies the board.

    Args:
        config: A stored ``source.config``.

    Returns:
        ``"board_token=stripe"``, or ``None`` when the config carries no key
        from :data:`IDENTITY_KEYS`. The whole JSONB is deliberately not
        summarised: a list row wants the value that moved, not the settings.
    """
    for key in IDENTITY_KEYS:
        value = config.get(key)
        if value is not None and value != "":
            return f"{key}={value}"
    return None


def redact_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a config safe to print in full.

    No Phase 1 adapter puts a credential in ``source.config`` — every one of them
    reads a public endpoint. This does not assume that stays true: any key whose
    *name* looks credential-shaped (the same pattern the log scrubber uses) has
    its value masked, and only the public board identifiers of
    :data:`IDENTITY_KEYS` are exempt.

    Args:
        config: A stored ``source.config``.

    Returns:
        The config with sensitive values masked, key-sorted.
    """
    return {
        key: (config[key] if key in IDENTITY_KEYS or not REDACT_KEY_RE.search(key) else REDACTED)
        for key in sorted(config)
    }


def source_probe_url(adapter: AtsType, config: Mapping[str, Any]) -> str | None:
    """Return the URL a probe of this source would request.

    Derived from the adapter's own ``URL_TEMPLATE`` so there is one definition of
    where an adapter points, and the policy gate checks the same string the
    adapter will later build.

    Args:
        adapter: The adapter type.
        config: The stored config.

    Returns:
        An absolute ``https`` URL, or ``None`` when this adapter fetches no board
        — ``mail_alert`` reads a mailbox, ``manual`` has no adapter at all.
    """
    try:
        adapter_cls = get_adapter(adapter)
    except AdapterConfigError:
        return None
    template = getattr(adapter_cls, "URL_TEMPLATE", None)
    if not isinstance(template, str):
        return None
    try:
        return f"https://{template.format(**dict(config))}"
    except (KeyError, IndexError):
        return None


@dataclass(frozen=True, slots=True)
class SourceDeletion:
    """What a :func:`delete_source` destroyed.

    Attributes:
        source_id: The source that is gone.
        company_id: The company it belonged to.
        adapter: Its adapter type.
        postings_deleted: How many ``job_posting`` rows the ``ON DELETE CASCADE``
            took with it (DATA_MODEL.md §4.1) — open, closed and filtered alike.
    """

    source_id: int
    company_id: int
    adapter: AtsType
    postings_deleted: int


@dataclass(frozen=True, slots=True)
class SourceProbe:
    """One re-probe of an existing source.

    Attributes:
        source_id: The source probed.
        adapter: Its adapter type.
        describe: The adapter's short human string.
        url: The URL the probe requested.
        probe: The adapter's own result, verbatim.
    """

    source_id: int
    adapter: AtsType
    describe: str
    url: str
    probe: ProbeResult


async def count_source_postings(session: AsyncSession, source_id: int) -> int:
    """Count every posting row a delete of this source would destroy.

    Deliberately not restricted to open postings: the cascade takes closed and
    filtered rows too, and the whole point of showing the number before a delete
    is that it is the real one.

    Args:
        session: An open session.
        source_id: The source.

    Returns:
        The row count.
    """
    total = await session.scalar(
        select(func.count()).select_from(JobPosting).where(JobPosting.source_id == source_id)
    )
    return int(total or 0)


async def open_posting_counts(
    session: AsyncSession, source_ids: Sequence[int] | None = None
) -> dict[int, int]:
    """Count open, filtered-in postings per source.

    Args:
        session: An open session.
        source_ids: Restrict to these sources; every source when omitted.

    Returns:
        ``{source_id: count}``, with sources that have none simply absent.
    """
    if source_ids is not None and not source_ids:
        return {}
    stmt = (
        select(JobPosting.source_id, func.count())
        .where(JobPosting.closed_at.is_(None), JobPosting.filtered_out.is_(False))
        .group_by(JobPosting.source_id)
    )
    if source_ids is not None:
        stmt = stmt.where(JobPosting.source_id.in_(sorted(set(source_ids))))
    result = await session.execute(stmt)
    return {int(source_id): int(count) for source_id, count in result.all()}


async def retire_source(session: AsyncSession, source_id: int) -> Source:
    """Take a source out of service without destroying anything.

    The answer to a board that moved (COMPANY_REGISTRY.md §11.1 step 4). The
    source stops polling, its postings and their ``first_seen_at`` history
    survive, and the roles that genuinely moved collapse against the new board's
    rows by the cross-source dedup key. Nothing is lost, and it is reversible
    with ``source enable``.

    ``consecutive_failures`` is cleared because that counter exists only to drive
    the auto-disable threshold, and a retired source will not run again — leaving
    it set would keep a decided board in the "needs attention" list forever.
    ``last_error`` is kept: it is the record of *why* the board was retired, and
    six months later it is the only one.

    Args:
        session: An open session. The caller owns the transaction.
        source_id: The source to retire.

    Returns:
        The updated source.

    Raises:
        SourceNotFound: When the source does not exist.
    """
    source = await require_source(session, source_id)
    source.enabled = False
    source.last_status = RETIRED_STATUS
    source.consecutive_failures = 0
    await session.flush()
    log.info(
        "source_retired",
        source_id=source.id,
        company_id=source.company_id,
        adapter=source.adapter.value,
    )
    return source


async def delete_source(session: AsyncSession, source_id: int) -> SourceDeletion:
    """Delete a source and, by cascade, every posting ever seen through it.

    ``job_posting.source_id`` is ``ON DELETE CASCADE`` (DATA_MODEL.md §4.1), so
    this destroys discovery history that nothing can reconstruct.
    :func:`retire_source` is the right call for a board that moved; this is for
    the mistyped config that never successfully ran.

    The count is taken inside the caller's transaction, so the number reported is
    the number destroyed rather than a number read a moment earlier.

    Args:
        session: An open session. The caller owns the transaction.
        source_id: The source to delete.

    Returns:
        What was destroyed.

    Raises:
        SourceNotFound: When the source does not exist.
    """
    source = await require_source(session, source_id)
    postings = await count_source_postings(session, source_id)
    deletion = SourceDeletion(
        source_id=source.id,
        company_id=source.company_id,
        adapter=source.adapter,
        postings_deleted=postings,
    )
    await session.delete(source)
    await session.flush()
    log.warning(
        "source_deleted",
        source_id=deletion.source_id,
        company_id=deletion.company_id,
        adapter=deletion.adapter.value,
        postings_deleted=deletion.postings_deleted,
    )
    return deletion


async def probe_source(
    session: AsyncSession,
    source_id: int,
    *,
    settings: Settings | None = None,
    redis: Redis | None = None,
) -> SourceProbe:
    """Re-probe an existing source with the adapter that reads it.

    The same one-request probe detection runs (§2.3), against the config already
    stored. The order matches :func:`detect_ats` and matters for the same reason:
    the config is validated, the target URL is derived, and the never-fetch gate
    runs on that URL **before** an HTTP client exists. A denied host is refused
    without a request having been made.

    This is read-only. It does not clear ``consecutive_failures`` and does not
    re-enable anything: re-enabling is its own command, so that testing a source
    the operator deliberately retired cannot silently put it back into the
    nightly run.

    Args:
        session: An open session, used only to load the source.
        source_id: The source to probe.
        settings: Configuration; resolved from the environment when omitted.
        redis: Redis, for the shared token bucket and the robots cache. Without
            it the probe still honours robots.txt and still refuses never-fetch
            hosts; it simply does not share a bucket with a concurrent run, which
            is the same trade :func:`detect_for_url` makes for one request.

    Returns:
        The probe result and what was probed.

    Raises:
        SourceNotFound: When the source does not exist.
        SourceNotProbeable: When this adapter fetches no board URL.
        AdapterConfigError: When the stored config no longer validates.
        DeniedByPolicy: When the derived URL is on the never-fetch list. No
            request is made, and this is not overridable.
    """
    source = await require_source(session, source_id)
    adapter_cls = get_adapter(source.adapter)
    config = adapter_cls.parse_config(dict(source.config or {}))

    url = source_probe_url(source.adapter, source.config or {})
    if url is None:
        raise SourceNotProbeable(
            f"source {source_id} is a {source.adapter.value} source, which fetches no board URL; "
            "there is nothing to re-probe"
        )
    assert_fetch_allowed(url)

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
        http = build_source_client(
            client,
            settings=resolved,
            source_id=source.id,
            adapter=source.adapter,
            bucket_key=STATIC_BUCKET_KEYS.get(source.adapter) or httpx.URL(url).host,
            limiter=limiter,
            robots=robots,
            breaker=InRunCircuitBreaker(threshold=resolved.circuit_breaker_failures),
        )
        adapter = adapter_cls(source_id=source.id, config=config, http=http)
        try:
            result = await adapter.probe()
            describe = adapter.describe()
        finally:
            await adapter.aclose()
    finally:
        await client.aclose()

    log.info(
        "source_probed",
        source_id=source.id,
        adapter=source.adapter.value,
        host=httpx.URL(url).host,
        reachable=result.reachable,
        http_status=result.http_status,
    )
    return SourceProbe(
        source_id=source.id,
        adapter=source.adapter,
        describe=describe,
        url=url,
        probe=result,
    )


async def touch_company_seen(session: AsyncSession, company_id: int) -> None:
    """Record that a company was reviewed, without changing anything else."""
    await session.execute(
        update(Company).where(Company.id == company_id).values(updated_at=utcnow())
    )


__all__ = [
    "CONFIG_MODELS",
    "IDENTITY_KEYS",
    "PATTERNS",
    "RETIRED_STATUS",
    "TAG_AXES",
    "DetectionResult",
    "DuplicateSource",
    "PatternMatch",
    "RegistryError",
    "SourceDeletion",
    "SourceNotFound",
    "SourceNotProbeable",
    "SourceProbe",
    "TagInvalid",
    "Undetectable",
    "canonical_config",
    "companies_by_id",
    "config_identity",
    "count_source_postings",
    "create_company",
    "create_source",
    "delete_source",
    "detect_ats",
    "detect_for_url",
    "find_source",
    "get_company",
    "get_company_by_slug",
    "get_source",
    "list_companies",
    "list_sources",
    "match_url",
    "normalise_url",
    "open_posting_counts",
    "probe_source",
    "redact_config",
    "require_source",
    "resolve_company",
    "retire_source",
    "set_company_status",
    "set_source_enabled",
    "slugify",
    "source_probe_url",
    "sources_query",
    "touch_company_seen",
    "validate_tags",
]
