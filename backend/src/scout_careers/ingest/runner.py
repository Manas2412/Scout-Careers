"""Stage ①: the discovery run, and the whole of invariant 5.

One broken source degrades that source only. A run always completes and always
reports which sources failed (ARCHITECTURE.md §3). Everything in this module
exists to make that true when it is inconvenient:

- ``asyncio.gather(..., return_exceptions=True)`` — the invariant, in one
  keyword. A source that raises returns an exception object instead of tearing
  down its siblings.
- Two layers of catching in :func:`fetch_one`. The inner one classifies known
  failures into a status; the outer one guarantees that an unknown failure *in
  the classifier itself* is still recorded rather than raised. It should be
  unreachable. It is written anyway, because the code that upholds invariant 5
  is code, and code breaks.
- ``asyncio.CancelledError`` is re-raised everywhere, never swallowed. The
  whole-run budget must be able to stop the run, and a task that eats its own
  cancellation is a task that runs forever.
- A source cancelled by the 180-second ceiling is recorded as ``timeout`` and
  **discards everything it had yielded**. Postings are buffered per source and
  persisted only on clean completion: a half-fetched board looks like
  "everything else closed" to the two-run rule and would close live postings
  (SOURCE_ADAPTERS.md §4.4).
- ``run_log.status`` is ``failed`` only when the run could not proceed — Redis
  or Postgres unavailable, the lock unheld, the runner itself raising. Adapter
  failures produce ``completed_with_errors``, forever (§10.4).

The Redis run lock is a lock, not a queue. A second concurrent run is refused
with :class:`RunAlreadyInFlight`, because two runs writing the same
``(source_id, external_id)`` rows would race the close rule against itself. And
Redis being unavailable **refuses the run** rather than degrading it: without
Redis there is no lock, no shared token bucket and no robots cache, and a run
that quietly proceeds without rate limiting is exactly the failure mode
invariant 8 exists to prevent.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scout_careers.common.clock import utcnow
from scout_careers.common.config import Settings, get_settings
from scout_careers.common.errors import (
    AdapterConfigError,
    CircuitOpen,
    DeniedByPolicy,
    RateLimitTimeout,
    RobotsDenied,
    SchemaDriftError,
    ScoutError,
    SourceTimeout,
    TransportError,
    UpstreamHttpError,
)
from scout_careers.common.logging import get_logger
from scout_careers.common.text import truncate
from scout_careers.common.types import (
    NON_FAULT_STATUSES,
    AtsType,
    CompanyStatus,
    RunStatus,
    SourceStatus,
)
from scout_careers.db.models import Company, JobPosting, RunLog, Source
from scout_careers.db.session import get_session_factory, session_scope
from scout_careers.ingest.close import close_missing
from scout_careers.ingest.dedup import collapse_duplicates
from scout_careers.ingest.persist import PersistCounts, persist_postings
from scout_careers.ingest.resolve import ALERT_RAW_FLAGS, resolve_alert_companies
from scout_careers.ingest.results import (
    RunStats,
    failure_summary,
    fold_results,
    resolve_run_status,
    source_results_payload,
)
from scout_careers.mail.gmail import build_mail_reader, close_mail_reader
from scout_careers.sources.base import MailReader, RawPosting, SourceAdapter, SourceResult
from scout_careers.sources.http import (
    InRunCircuitBreaker,
    RateLimiter,
    RedisTokenBucket,
    RobotsPolicy,
    SourceHttpClient,
    build_client,
    build_source_client,
)
from scout_careers.sources.registry import get_adapter

log = get_logger(__name__)

#: The run type written to ``run_log.run_type``.
RUN_TYPE = "discovery"

#: One discovery run at a time, system-wide.
RUN_LOCK_KEY = "lock:run:discovery"

#: HTTP 429. Retryable, and exhausting the retry budget on it means the board
#: asked us to slow down and we could not slow down enough.
HTTP_TOO_MANY_REQUESTS = 429


class RunRefused(ScoutError):
    """The run did not start. Base for the two refusal reasons."""

    error_code: ClassVar[str] = "run.refused"


class RunAlreadyInFlight(RunRefused):
    """Another discovery run holds the lock. Refused, not queued.

    Mapped to HTTP 409 by the API (``API.md`` §7).
    """

    error_code: ClassVar[str] = "run.already_in_flight"


class RunLockUnavailable(RunRefused):
    """Redis could not be reached, so the run lock could not be held.

    This is a refusal, not a degradation: without Redis there is no lock, no
    shared rate-limit bucket and no robots cache.
    """

    error_code: ClassVar[str] = "run.lock_unavailable"


class MailReaderUnavailable(ScoutError):
    """A ``mail_alert`` source came due with no mailbox reader wired in.

    The ordinary state of an install that has not run ``scout-careers auth
    gmail`` yet, or one where ``MAIL_ENABLED`` is false. The source is reported
    ``disabled`` — not attempted, nobody's fault, no failure counted against a
    board that did nothing wrong — rather than ``error``, because an
    unauthorised mailbox is a configuration state, not a fault.
    """

    error_code: ClassVar[str] = "adapter.mail_reader_unavailable"


# ---------------------------------------------------------------------------
# What the runner carries about a source
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DueSource:
    """A source selected for this run, snapshotted out of the ORM.

    A plain value rather than a ``Source`` instance on purpose: each source is
    persisted in its own session, and handing a second session an instance
    loaded by the first is how a detached-instance error arrives at 02:30.
    """

    id: int
    company_id: int
    adapter: AtsType
    config: dict[str, Any]
    enabled: bool = True
    poll_interval_minutes: int = 1440

    def describe(self) -> str:
        """Return a stand-in description for a source whose adapter never built."""
        return f"{self.adapter.value} · source {self.id}"


@dataclass(slots=True)
class SourceOutcome:
    """One source's result plus what its persistence did.

    Attributes:
        result: The ``run_log.source_results`` entry.
        closed: Postings closed by the two-run rule for this source.
    """

    result: SourceResult
    closed: int = 0


@dataclass(slots=True)
class RunOutcome:
    """What a completed run reports to its caller.

    Attributes:
        run_id: The run's ULID.
        status: ``completed`` or ``completed_with_errors``.
        stats: The ``run_log.stats`` document.
        results: One entry per attempted source.
    """

    run_id: str
    status: RunStatus
    stats: RunStats
    results: list[SourceResult]


# ---------------------------------------------------------------------------
# The run lock
# ---------------------------------------------------------------------------


class RunLock(Protocol):
    """A mutually exclusive hold on the discovery run."""

    async def acquire(self) -> bool:
        """Try to take the lock.

        Returns:
            True when taken, False when another holder has it.

        Raises:
            RedisError: When the lock store is unreachable.
        """

    async def release(self) -> None:
        """Release the lock if this holder still owns it."""


class RedisRunLock:
    """``lock:run:discovery``, held for ``RUN_LOCK_TTL_S``.

    The TTL is the crash guard: a runner killed mid-run leaves the key behind,
    and configuration refuses to boot unless the TTL exceeds the whole-run
    budget (``common/config.py``), so the lock cannot expire under a run that is
    still working.
    """

    #: Lua so that expiry between the read and the delete cannot make one holder
    #: release another's lock.
    RELEASE_SCRIPT: ClassVar[str] = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    def __init__(self, redis: Redis, *, ttl_s: int, key: str = RUN_LOCK_KEY) -> None:
        self._redis = redis
        self._ttl_s = ttl_s
        self._key = key
        self._token = uuid.uuid4().hex
        self._held = False

    async def acquire(self) -> bool:
        """Take the lock with ``SET NX EX``.

        Returns:
            True when this process now holds it.

        Raises:
            RedisError: When Redis is unreachable. The caller turns that into a
                refusal, never into a run that proceeds without a lock.
        """
        acquired = await self._redis.set(self._key, self._token, nx=True, ex=self._ttl_s)
        self._held = bool(acquired)
        return self._held

    async def release(self) -> None:
        """Release the lock, tolerating a Redis that went away mid-run."""
        if not self._held:
            return
        self._held = False
        try:
            await self._redis.eval(self.RELEASE_SCRIPT, 1, self._key, self._token)
        except RedisError:
            # The TTL will clear it. Failing to release is not worth failing an
            # otherwise complete run over.
            log.warning("run_lock_release_failed", key=self._key)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


class SourcePersister(Protocol):
    """Persists one source's buffered postings in its own transaction."""

    async def __call__(
        self,
        *,
        source: DueSource,
        postings: Sequence[RawPosting],
        result: SourceResult,
        settings: Settings,
    ) -> SourceOutcome:
        """Write the source's rows and record its outcome.

        Returns:
            The result, with change-detection counts filled in, and the number
            of postings the close rule closed.
        """


AdapterFactory = Callable[[DueSource, SourceHttpClient | None], SourceAdapter]


@dataclass(slots=True)
class RunnerDeps:
    """Everything the runner needs that is not the database session.

    Injectable in full, because the isolation tests have to be able to make an
    adapter explode without a network, a Redis or a Postgres in the room.
    """

    settings: Settings
    redis: Redis | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None
    lock: RunLock | None = None
    limiter: RateLimiter | None = None
    client_factory: Callable[[Settings], httpx.AsyncClient] = build_client
    adapter_factory: AdapterFactory | None = None
    persist: SourcePersister | None = None
    mail_reader: MailReader | None = None
    on_result: Callable[[SourceResult], None] | None = None
    dedupe: bool = True

    @classmethod
    def build(cls, settings: Settings | None = None) -> RunnerDeps:
        """Build the production dependency set.

        The mailbox reader is wired here, and only here. When Gmail is
        configured, ``mail_alert`` sources get a real ``GmailClient``; when it is
        not, ``mail_reader`` stays ``None`` and the runner reports those sources
        as ``disabled`` — not attempted, nobody's fault, no failure counted
        against a board that did nothing wrong.

        This is also the one place ``mail/`` and ``sources/`` meet. ``sources/``
        owns the ``MailReader`` protocol and imports nothing from ``mail/``;
        ``mail/`` implements the protocol and imports nothing back. The
        injection is what keeps that true.

        Args:
            settings: Configuration; resolved from the environment when omitted.

        Returns:
            Dependencies wired to the real Redis, session factory, HTTP client
            and — when authorised — mailbox.
        """
        resolved = settings or get_settings()
        redis: Redis = Redis.from_url(
            str(resolved.redis_url),
            max_connections=resolved.redis_max_connections,
            socket_timeout=resolved.redis_socket_timeout_s,
            decode_responses=True,
        )
        return cls(
            settings=resolved,
            redis=redis,
            mail_reader=build_mail_reader(resolved, redis),
        )

    async def aclose(self) -> None:
        """Close anything this dependency set owns. Safe to call twice."""
        await close_mail_reader(self.mail_reader)
        self.mail_reader = None
        if self.redis is not None:
            await self.redis.aclose()


@dataclass(slots=True)
class _RunContext:
    """Per-run objects shared by every source."""

    deps: RunnerDeps
    settings: Settings
    client: httpx.AsyncClient
    robots: RobotsPolicy
    breaker: InRunCircuitBreaker
    limiter: RateLimiter
    run_id: str


# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------


async def load_due_sources(
    session: AsyncSession,
    source_ids: Sequence[int] | None = None,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> list[DueSource]:
    """Select the sources this run will attempt.

    Three conditions, and the third is the only one an explicit ``source_ids``
    list or ``force`` relaxes:

    1. ``source.enabled`` is true. A source the operator or the auto-disable
       threshold switched off stays off.
    2. The company is not ``blacklisted`` (and not soft-deleted). A blacklist is
       a decision about an employer, and a decision that a re-run argument can
       bypass is not a decision (COMPANY_REGISTRY.md §4.1).
    3. It is due: ``last_run_at + poll_interval_minutes`` has passed, or it has
       never run.

    Naming an explicit list means "run these now" — that is the entire purpose
    of ``POST /runs/discovery {"source_ids": [...]}`` after fixing a board token
    — so it bypasses the poll interval. It does not, and cannot, bypass 1 or 2.

    ``force`` is the same relaxation for every source rather than a named few.
    It exists because the interval is a *scheduling* rule, not a policy one:
    after fixing an adapter you want the whole registry re-fetched now, and
    "wait until tomorrow, or type out forty-three ids" is not a real choice. It
    is off by default, so the scheduled run is never accidentally a full sweep,
    and it is equally unable to bypass 1 or 2.

    Args:
        session: The run's session.
        source_ids: Restrict to these sources, and ignore the poll interval.
        now: The comparison clock; defaults to the current UTC time.
        force: Ignore the poll interval for every source that passes 1 and 2.

    Returns:
        The due sources, ordered by id so a run is reproducible.
    """
    moment = now or utcnow()
    stmt = (
        select(Source)
        .join(Company, Company.id == Source.company_id)
        .where(
            Source.enabled.is_(True),
            Company.status != CompanyStatus.BLACKLISTED,
            Company.deleted_at.is_(None),
        )
        .order_by(Source.id)
    )
    if source_ids is not None:
        stmt = stmt.where(Source.id.in_(list(source_ids)))
    elif not force:
        due_at = Source.last_run_at + func.make_interval(
            0, 0, 0, 0, 0, Source.poll_interval_minutes
        )
        stmt = stmt.where(or_(Source.last_run_at.is_(None), due_at <= moment))

    rows = await session.execute(stmt)
    return [
        DueSource(
            id=source.id,
            company_id=source.company_id,
            adapter=source.adapter,
            config=dict(source.config or {}),
            enabled=source.enabled,
            poll_interval_minutes=source.poll_interval_minutes,
        )
        for source in rows.scalars().all()
    ]


# ---------------------------------------------------------------------------
# Failure classification (§10.2)
# ---------------------------------------------------------------------------


def classify_failure(exc: BaseException) -> tuple[SourceStatus, str, str]:
    """Map an exception to a status, a curated message and an error code.

    The message is the exception's own text, which every ``ScoutError`` in this
    codebase constructs from identifiers and never from a response body. An
    exception from outside the hierarchy contributes its *type* only: its text
    may quote an upstream body, and upstream bodies are untrusted input that is
    rendered in the digest.

    Args:
        exc: The exception that ended the source's fetch.

    Returns:
        ``(status, error, error_code)``.
    """
    if isinstance(exc, DeniedByPolicy):
        return SourceStatus.DENIED_BY_POLICY, exc.message, exc.error_code
    if isinstance(exc, RobotsDenied):
        return SourceStatus.ROBOTS_DENIED, exc.message, exc.error_code
    if isinstance(exc, RateLimitTimeout):
        return SourceStatus.RATE_LIMITED, exc.message, exc.error_code
    if isinstance(exc, CircuitOpen):
        return SourceStatus.CIRCUIT_OPEN, exc.message, exc.error_code
    if isinstance(exc, SchemaDriftError):
        return SourceStatus.SCHEMA_ERROR, exc.message, exc.error_code
    if isinstance(exc, SourceTimeout):
        return SourceStatus.TIMEOUT, exc.message, exc.error_code
    if isinstance(exc, MailReaderUnavailable):
        return SourceStatus.DISABLED, exc.message, exc.error_code
    if isinstance(exc, AdapterConfigError):
        # A config that does not validate and a board that is not there are the
        # same fault from the operator's seat, and the same remedy: fix the
        # config (``common/errors.py``).
        return SourceStatus.HTTP_ERROR, exc.message, exc.error_code
    if isinstance(exc, UpstreamHttpError):
        if exc.status_code == HTTP_TOO_MANY_REQUESTS:
            # The board asked us to slow down and we could not slow down enough
            # inside the retry budget. Counting that against it would auto-disable
            # healthy boards during a busy run — the thing §4.8 forbids.
            return SourceStatus.RATE_LIMITED, exc.message, RateLimitTimeout.error_code
        if httpx.codes.BAD_REQUEST <= exc.status_code < httpx.codes.INTERNAL_SERVER_ERROR:
            return SourceStatus.HTTP_ERROR, exc.message, exc.error_code
        return SourceStatus.ERROR, exc.message, exc.error_code
    if isinstance(exc, TransportError):
        return SourceStatus.ERROR, exc.message, exc.error_code
    if isinstance(exc, ScoutError):
        return SourceStatus.ERROR, exc.message, exc.error_code
    return SourceStatus.ERROR, f"unexpected {type(exc).__name__}", "adapter.unknown"


# ---------------------------------------------------------------------------
# Per-source execution
# ---------------------------------------------------------------------------


def build_adapter(
    source: DueSource,
    http: SourceHttpClient | None,
    *,
    mail_reader: MailReader | None = None,
) -> SourceAdapter:
    """Construct the adapter for a source.

    ``mail_alert`` is constructed differently on purpose: it issues no HTTP
    requests at all, so it is handed a mailbox reader rather than an HTTP
    client. Giving it one would be handing it a capability it must not hold
    (SOURCE_ADAPTERS.md §7).

    Args:
        source: The source being run.
        http: Its per-source HTTP facade, or ``None`` for ``mail_alert``.
        mail_reader: The mailbox reader, for ``mail_alert`` only.

    Returns:
        A constructed adapter.

    Raises:
        AdapterConfigError: When no adapter is registered for the type, or the
            stored config no longer validates. Re-validating at the top of every
            run is deliberate: a config that rotted since it was saved fails
            loudly rather than silently.
        MailReaderUnavailable: When a ``mail_alert`` source has no reader.
    """
    adapter_cls = get_adapter(source.adapter)
    config = adapter_cls.parse_config(source.config)

    if source.adapter is AtsType.MAIL_ALERT:
        if mail_reader is None:
            raise MailReaderUnavailable(
                "no mailbox reader is configured; the mail_alert source was not attempted"
            )
        # The mail adapter's constructor takes ``mail`` where every other
        # adapter takes ``http``; the protocol describes the common case and the
        # registry is the only place that knows the difference.
        factory: Any = adapter_cls
        adapter: SourceAdapter = factory(source_id=source.id, config=config, mail=mail_reader)
        return adapter
    if http is None:  # pragma: no cover - guarded by the caller
        raise AdapterConfigError(f"{source.adapter.value} requires an HTTP client")
    return adapter_cls(source_id=source.id, config=config, http=http)


async def _collect(adapter: SourceAdapter, *, timeout_s: int) -> list[RawPosting]:
    """Drain an adapter's ``fetch()`` under the per-source ceiling.

    Args:
        adapter: The constructed adapter.
        timeout_s: ``settings.source_timeout_s`` — 180 by default.

    Returns:
        Every posting the adapter yielded.

    Raises:
        TimeoutError: When the ceiling expires. The caller discards the buffer;
            partial ingestion is not allowed.
    """
    buffered: list[RawPosting] = []
    async with asyncio.timeout(timeout_s):
        async for posting in adapter.fetch():
            buffered.append(posting)
    return buffered


async def _fetch_one(source: DueSource, *, ctx: _RunContext) -> SourceOutcome:
    """Run one source: construct, fetch, classify, persist.

    Args:
        source: The source to run.
        ctx: The run's shared objects.

    Returns:
        Its outcome. Every failure short of cancellation becomes a status here.

    Raises:
        asyncio.CancelledError: Always re-raised. The whole-run budget must be
            able to stop this.
    """
    started = time.monotonic()
    http: SourceHttpClient | None = None
    adapter: SourceAdapter | None = None
    describe = source.describe()
    postings: list[RawPosting] = []
    status = SourceStatus.OK
    error: str | None = None
    error_code: str | None = None

    try:
        if source.adapter is not AtsType.MAIL_ALERT:
            http = build_source_client(
                ctx.client,
                settings=ctx.settings,
                source_id=source.id,
                adapter=source.adapter,
                bucket_key=None,
                limiter=ctx.limiter,
                robots=ctx.robots,
                breaker=ctx.breaker,
            )
        factory = ctx.deps.adapter_factory
        adapter = (
            factory(source, http)
            if factory is not None
            else build_adapter(source, http, mail_reader=ctx.deps.mail_reader)
        )
        describe = adapter.describe()
        postings = await _collect(adapter, timeout_s=ctx.settings.source_timeout_s)
        status = SourceStatus.OK if postings else SourceStatus.EMPTY
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        # Everything this source had yielded is dropped on the floor. A
        # half-fetched board would look like "everything else closed" to the
        # two-run rule and would close live postings (§4.4).
        postings = []
        status = SourceStatus.TIMEOUT
        error = f"source exceeded the {ctx.settings.source_timeout_s}s ceiling"
        error_code = SourceTimeout.error_code
        log.warning("source_timeout", run_id=ctx.run_id, source_id=source.id)
    except Exception as exc:  # the inner classifier (§10.1)
        postings = []
        status, error, error_code = classify_failure(exc)
        log.warning(
            "source_failed",
            run_id=ctx.run_id,
            source_id=source.id,
            adapter=source.adapter.value,
            status=status.value,
            error_code=error_code,
            exc_info=not isinstance(exc, ScoutError),
        )
    finally:
        if adapter is not None:
            await adapter.aclose()

    counters = http.counters if http is not None else None
    result = SourceResult(
        source_id=source.id,
        company_id=source.company_id,
        adapter=source.adapter,
        describe=describe,
        status=status,
        fetched=len(postings),
        skipped=dict(getattr(adapter, "skipped", {}) or {}),
        duration_ms=int((time.monotonic() - started) * 1000),
        requests=counters.requests if counters else 0,
        retries=counters.retries if counters else 0,
        rate_limit_wait_ms=counters.rate_limit_wait_ms if counters else 0,
        error=truncate(error, 500),
        error_code=error_code,
    )

    persist = ctx.deps.persist or persist_source
    return await persist(source=source, postings=postings, result=result, settings=ctx.settings)


async def fetch_one(source: DueSource, *, ctx: _RunContext) -> SourceOutcome:
    """Run one source, and never raise anything but cancellation.

    The outer of the two layers §10.1 calls for. :func:`_fetch_one` already
    converts every exception into a status; if *it* fails, that failure is
    recorded as a ``SourceResult`` rather than allowed to reach ``gather``.

    Args:
        source: The source to run.
        ctx: The run's shared objects.

    Returns:
        Its outcome, whatever happened.

    Raises:
        asyncio.CancelledError: Always re-raised.
    """
    started = time.monotonic()
    try:
        return await _fetch_one(source, ctx=ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # the outer guarantee (§10.1)
        log.exception(
            "source_runner_crashed",
            run_id=ctx.run_id,
            source_id=source.id,
            adapter=source.adapter.value,
        )
        return SourceOutcome(
            result=SourceResult.crashed(
                source_id=source.id,
                company_id=source.company_id,
                adapter=source.adapter,
                describe=source.describe(),
                exc=exc,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )


# ---------------------------------------------------------------------------
# Persistence and the durable breaker
# ---------------------------------------------------------------------------


async def record_source_outcome(
    session: AsyncSession,
    *,
    source_id: int,
    result: SourceResult,
    now: datetime,
    auto_disable_threshold: int,
) -> None:
    """Update the source's health after a run (SOURCE_ADAPTERS.md §4.8).

    The cross-run circuit breaker. Success clears history — a source that works
    today is not on probation for a transient outage last week. Failure counts,
    and five consecutive failures disable the source, because five consecutive
    daily failures almost always means the board moved and needs a new config,
    not that the network was unlucky five times.

    ``rate_limited`` and ``circuit_open`` are our own back-pressure, not the
    source's fault, and do not count. ``robots_denied`` and ``denied_by_policy``
    disable immediately: they are refusals, and retrying a refusal four more
    times is four more requests we were told not to make.

    Args:
        session: The session this source's work runs in.
        source_id: The source to update.
        result: Its outcome.
        now: Written to ``last_run_at``.
        auto_disable_threshold: ``settings.auto_disable_threshold``.
    """
    values: dict[str, Any] = {"last_run_at": now, "last_status": result.status.value}

    if result.status in (SourceStatus.OK, SourceStatus.EMPTY):
        values.update(consecutive_failures=0, last_error=None)
    elif result.status in NON_FAULT_STATUSES:
        # rate_limited, circuit_open, disabled: recorded, not counted.
        values.update(last_error=truncate(result.error, 500))
    else:
        source = await session.get(Source, source_id)
        failures = (source.consecutive_failures if source is not None else 0) + 1
        refused = result.status in (SourceStatus.ROBOTS_DENIED, SourceStatus.DENIED_BY_POLICY)
        disable = refused or failures >= auto_disable_threshold
        values.update(
            consecutive_failures=failures,
            # Never re-enables: only an already-enabled source runs, and
            # re-enabling is a human act (§4.8).
            enabled=(source.enabled if source is not None else True) and not disable,
            last_status="auto_disabled" if disable and not refused else result.status.value,
            last_error=truncate(result.error, 500),
        )
        if disable:
            log.warning(
                "source_auto_disabled",
                source_id=source_id,
                status=result.status.value,
                consecutive_failures=failures,
            )

    await session.execute(update(Source).where(Source.id == source_id).values(**values))


async def persist_source(
    *,
    source: DueSource,
    postings: Sequence[RawPosting],
    result: SourceResult,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> SourceOutcome:
    """Persist one source's postings in a transaction of its own.

    Per source, not per run: a constraint violation on one board must not roll
    back another board's rows, and one session shared by eight concurrent tasks
    is a SQLAlchemy error waiting to happen.

    Args:
        source: The source that ran.
        postings: Its complete buffered output; empty when it failed.
        result: Its result so far, mutated in place with the change counts.
        settings: Configuration.
        session_factory: Session maker; the process-wide one when omitted.

    Returns:
        The outcome, with counts and the close-rule tally filled in.
    """
    seen_at = utcnow()
    counts = PersistCounts()
    closed = 0

    async with session_scope(session_factory or get_session_factory()) as session:
        if postings:
            # A board's source is an employer, so `source.company_id` is the
            # answer for every adapter but one. A mail alert is a mailbox
            # carrying roles at many employers; SOURCE_ADAPTERS.md §7.3 says who
            # each belongs to, and ingest/resolve.py works it out.
            overrides: Mapping[str, int] | None = None
            raw_extra: Mapping[str, Any] | None = None
            if source.adapter is AtsType.MAIL_ALERT:
                overrides = await resolve_alert_companies(
                    session,
                    postings,
                    threshold=settings.alert_company_match_threshold,
                    source_id=source.id,
                )
                raw_extra = ALERT_RAW_FLAGS

            counts = await persist_postings(
                session,
                postings,
                source_id=source.id,
                company_id=source.company_id,
                now=seen_at,
                max_description_chars=settings.max_description_chars,
                company_overrides=overrides,
                raw_extra=raw_extra,
            )
        closed = await close_missing(
            session,
            source_id=source.id,
            status=result.status,
            seen_at=seen_at,
            threshold=settings.ingest_close_after_missed_runs,
        )
        await record_source_outcome(
            session,
            source_id=source.id,
            result=result,
            now=seen_at,
            auto_disable_threshold=settings.auto_disable_threshold,
        )

    result.new = counts.new
    result.updated = counts.updated
    result.unchanged = counts.unchanged
    return SourceOutcome(result=result, closed=closed)


# ---------------------------------------------------------------------------
# run_log bookkeeping
# ---------------------------------------------------------------------------


async def _start_run(session: AsyncSession, run_id: str, started_at: datetime) -> None:
    """Insert the ``running`` row, and commit it so the run is visible mid-flight."""
    session.add(
        RunLog(
            id=run_id,
            run_type=RUN_TYPE,
            status=RunStatus.RUNNING,
            started_at=started_at,
            stats={},
            source_results=[],
        )
    )
    await session.commit()


async def _finalise_run(
    session: AsyncSession,
    run_id: str,
    *,
    status: RunStatus,
    stats: RunStats | None = None,
    results: Sequence[SourceResult] = (),
    error: str | None = None,
) -> None:
    """Write the terminal ``run_log`` row.

    Args:
        session: The run's session.
        run_id: The run being finalised.
        status: The resolved run status.
        stats: The folded statistics, if the run got that far.
        results: Per-source results for ``source_results``.
        error: A curated one-line summary for ``run_log.error``.
    """
    await session.execute(
        update(RunLog)
        .where(RunLog.id == run_id)
        .values(
            status=status,
            finished_at=utcnow(),
            stats=(stats or RunStats()).model_dump(mode="json"),
            source_results=source_results_payload(results),
            error=truncate(error, 2_000),
        )
    )
    await session.commit()


async def _fail_run(session: AsyncSession, run_id: str, error: str) -> None:
    """Record a run that could not proceed, tolerating a database that is why."""
    try:
        existing = await session.get(RunLog, run_id)
        if existing is None:
            session.add(
                RunLog(
                    id=run_id,
                    run_type=RUN_TYPE,
                    status=RunStatus.FAILED,
                    started_at=utcnow(),
                    finished_at=utcnow(),
                    stats={},
                    source_results=[],
                    error=truncate(error, 2_000),
                )
            )
            await session.commit()
            return
        await _finalise_run(session, run_id, status=RunStatus.FAILED, error=error)
    except Exception:  # the database may be the failure itself
        log.exception("run_failure_not_recorded", run_id=run_id)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def run_discovery(
    session: AsyncSession,
    run_id: str,
    source_ids: Sequence[int] | None = None,
    *,
    deps: RunnerDeps | None = None,
    force: bool = False,
) -> RunOutcome:
    """Run stage ① for every due source, and report what happened.

    Args:
        session: The run's session, used for source selection, the ``run_log``
            row and the cross-source collapse. Each source's own writes go
            through their own session.
        run_id: A ULID, generated by the caller so the API can return it before
            the run finishes.
        source_ids: Restrict the run to these sources; see
            :func:`load_due_sources` for what that does and does not relax.
        deps: Injected dependencies; the production set when omitted.
        force: Ignore every source's poll interval; see
            :func:`load_due_sources`.

    Returns:
        The run outcome.

    Raises:
        RunAlreadyInFlight: Another discovery run holds the lock. Refused, not
            queued.
        RunLockUnavailable: Redis is unreachable, so no lock can be held.
        Exception: Anything the runner itself raises, after the run has been
            marked ``failed``. Adapter failures never reach here.
    """
    resolved = deps or RunnerDeps.build()
    lock = resolved.lock or _default_lock(resolved)

    try:
        acquired = await lock.acquire()
    except RedisError as exc:
        await _fail_run(session, run_id, "redis unavailable: the run lock could not be held")
        raise RunLockUnavailable("redis unavailable; the discovery run was refused") from exc

    if not acquired:
        raise RunAlreadyInFlight("a discovery run is already in flight")

    started_at = utcnow()
    started = time.monotonic()
    try:
        await _start_run(session, run_id, started_at)
        outcome = await _execute(
            session, run_id, source_ids, deps=resolved, started=started, force=force
        )
    except (RunRefused, asyncio.CancelledError):
        raise
    except Exception as exc:
        # The runner itself broke. This — and Postgres or Redis being gone — is
        # the only thing that produces ``failed``.
        log.exception("run_failed", run_id=run_id)
        await _fail_run(session, run_id, f"run failed: {type(exc).__name__}")
        raise
    finally:
        await lock.release()

    return outcome


def _default_lock(deps: RunnerDeps) -> RunLock:
    """Build the Redis run lock, refusing the run when there is no Redis.

    Raises:
        RunLockUnavailable: When no Redis client was supplied. A run without a
            lock is not a degraded run, it is a second writer.
    """
    if deps.redis is None:
        raise RunLockUnavailable("no redis client; the discovery run was refused")
    return RedisRunLock(deps.redis, ttl_s=deps.settings.run_lock_ttl_s)


async def _execute(
    session: AsyncSession,
    run_id: str,
    source_ids: Sequence[int] | None,
    *,
    deps: RunnerDeps,
    started: float,
    force: bool = False,
) -> RunOutcome:
    """Fetch every due source concurrently, then collapse and finalise.

    Args:
        session: The run's session.
        run_id: The run's ULID.
        source_ids: Restrict to these sources, or None for the due set.
        deps: Resolved runner dependencies.
        started: Monotonic start time, for the duration stat.
        force: Ignore every source's poll interval; see
            :func:`load_due_sources`.

    Returns:
        The run outcome.
    """
    settings = deps.settings
    sources = await load_due_sources(session, source_ids, force=force)
    log.info("run_started", run_id=run_id, sources=len(sources))

    outcomes: dict[int, SourceOutcome] = {}
    if sources:
        client = deps.client_factory(settings)
        robots = RobotsPolicy(
            client,
            user_agent=settings.source_user_agent,
            cache_ttl_s=settings.robots_cache_ttl_s,
            redis=deps.redis,
        )
        limiter: RateLimiter = deps.limiter or _default_limiter(deps)
        ctx = _RunContext(
            deps=deps,
            settings=settings,
            client=client,
            robots=robots,
            breaker=InRunCircuitBreaker(threshold=settings.circuit_breaker_failures),
            limiter=limiter,
            run_id=run_id,
        )
        try:
            await _gather_sources(sources, ctx=ctx, outcomes=outcomes)
        finally:
            await client.aclose()

    results = [outcomes[source.id].result for source in sources if source.id in outcomes]
    closed = sum(outcome.closed for outcome in outcomes.values())

    superseded = 0
    if deps.dedupe and results:
        superseded = await collapse_duplicates(
            session, company_ids=sorted({source.company_id for source in sources})
        )
        await session.commit()

    stats = fold_results(
        results,
        duration_ms=int((time.monotonic() - started) * 1000),
        superseded=superseded,
        closed=closed,
    )
    status = resolve_run_status(results)
    await _finalise_run(
        session,
        run_id,
        status=status,
        stats=stats,
        results=results,
        error=failure_summary(results),
    )
    log.info(
        "run_finished",
        run_id=run_id,
        status=status.value,
        sources=stats.sources_total,
        failed=stats.sources_failed,
        fetched=stats.fetched,
        new=stats.new,
    )
    return RunOutcome(run_id=run_id, status=status, stats=stats, results=results)


def _default_limiter(deps: RunnerDeps) -> RateLimiter:
    """Build the shared token bucket, refusing the run when there is no Redis."""
    if deps.redis is None:
        raise RunLockUnavailable("no redis client; the discovery run was refused")
    return RedisTokenBucket(deps.redis)


async def _gather_sources(
    sources: Sequence[DueSource],
    *,
    ctx: _RunContext,
    outcomes: dict[int, SourceOutcome],
) -> None:
    """Run every source under the concurrency limit and the whole-run budget.

    Results are recorded as each source finishes rather than collected from
    ``gather``'s return value, so that a run cut short by the wall-clock budget
    still reports everything that completed before the axe fell.
    """
    semaphore = asyncio.Semaphore(ctx.settings.source_concurrency)

    async def one(source: DueSource) -> None:
        async with semaphore:
            outcome = await fetch_one(source, ctx=ctx)
        outcomes[source.id] = outcome
        if ctx.deps.on_result is not None:
            ctx.deps.on_result(outcome.result)

    tasks = [asyncio.create_task(one(source)) for source in sources]
    budget_expired = False
    try:
        async with asyncio.timeout(ctx.settings.run_wall_clock_budget_s):
            # return_exceptions=True is the invariant, in one keyword (§10.1).
            await asyncio.gather(*tasks, return_exceptions=True)
    except TimeoutError:
        budget_expired = True
        log.warning("run_budget_exceeded", run_id=ctx.run_id, sources=len(sources))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    for source in sources:
        if source.id in outcomes:
            continue
        outcomes[source.id] = SourceOutcome(
            result=_unfinished_result(source, budget_expired=budget_expired)
        )


def _unfinished_result(source: DueSource, *, budget_expired: bool) -> SourceResult:
    """Build the result for a source that never reported one.

    Either the whole-run budget cancelled it, or its task died in a way
    :func:`fetch_one` could not catch. Both are recorded, neither is raised.
    """
    if budget_expired:
        return SourceResult(
            source_id=source.id,
            company_id=source.company_id,
            adapter=source.adapter,
            describe=source.describe(),
            status=SourceStatus.TIMEOUT,
            error="cancelled by the whole-run wall-clock budget",
            error_code=SourceTimeout.error_code,
        )
    return SourceResult(
        source_id=source.id,
        company_id=source.company_id,
        adapter=source.adapter,
        describe=source.describe(),
        status=SourceStatus.ERROR,
        error="the source task ended without reporting a result",
        error_code="adapter.unknown",
    )


async def open_posting_count(session: AsyncSession) -> int:
    """Return how many postings are currently open, filtered in and unsuperseded.

    Args:
        session: Any session.

    Returns:
        The count the CLI prints after a run, so "did anything actually land"
        does not require a psql prompt.
    """
    total = await session.scalar(
        select(func.count())
        .select_from(JobPosting)
        .where(JobPosting.closed_at.is_(None), JobPosting.filtered_out.is_(False))
    )
    return int(total or 0)


__all__ = [
    "RUN_LOCK_KEY",
    "RUN_TYPE",
    "AdapterFactory",
    "DueSource",
    "MailReaderUnavailable",
    "RedisRunLock",
    "RunAlreadyInFlight",
    "RunLock",
    "RunLockUnavailable",
    "RunOutcome",
    "RunRefused",
    "RunnerDeps",
    "SourceOutcome",
    "SourcePersister",
    "build_adapter",
    "classify_failure",
    "fetch_one",
    "load_due_sources",
    "open_posting_count",
    "persist_source",
    "record_source_outcome",
    "run_discovery",
]
