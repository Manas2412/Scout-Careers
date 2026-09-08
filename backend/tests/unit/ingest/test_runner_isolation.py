"""Invariant 5: one broken source degrades that source only.

"A discovery run always completes and always reports which sources failed"
(ARCHITECTURE.md §3). These are the tests that make that a fact rather than an
intention, so they are deliberately adversarial: an adapter that raises, every
adapter raising, the classifier itself raising, and a source that never returns.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from scout_careers.common.clock import utcnow
from scout_careers.common.errors import (
    DeniedByPolicy,
    RateLimitTimeout,
    RobotsDenied,
    SchemaDriftError,
    TransportError,
    UpstreamHttpError,
)
from scout_careers.common.types import AtsType, RunStatus, SourceStatus
from scout_careers.ingest import runner as runner_module
from scout_careers.ingest.runner import (
    DueSource,
    RunAlreadyInFlight,
    RunLockUnavailable,
    RunnerDeps,
    _RunContext,
    classify_failure,
    fetch_one,
    record_source_outcome,
    run_discovery,
)
from scout_careers.sources.base import SourceResult
from scout_careers.sources.http import InRunCircuitBreaker, NullRateLimiter, RobotsPolicy
from tests.unit.ingest.conftest import (
    FakeAdapter,
    FakeLock,
    FakeSession,
    RecordingPersister,
    make_posting,
    make_source,
)


def _client(_settings: object) -> httpx.AsyncClient:
    """A real client that is never asked to make a request."""
    return httpx.AsyncClient()


def _deps(settings, *, adapters, persister=None, lock=None) -> RunnerDeps:
    """Build a fully injected dependency set: no Redis, no Postgres, no network.

    Args:
        settings: Test settings.
        adapters: ``source_id -> FakeAdapter`` or a callable raising for a source.
        persister: Records what would have been written.
        lock: The run lock; an always-acquiring fake by default.
    """

    def factory(source: DueSource, _http: object):
        entry = adapters[source.id]
        if isinstance(entry, BaseException):
            raise entry
        return entry

    return RunnerDeps(
        settings=settings,
        lock=lock or FakeLock(),
        limiter=NullRateLimiter(),
        client_factory=_client,
        adapter_factory=factory,
        persist=persister or RecordingPersister(),
        # Both of these do a second pass over the run's session after the
        # sources finish. These tests are about one broken adapter not taking
        # the others with it, and the fake session here answers `execute` and
        # nothing else — standing up one that can also stream postings would be
        # testing persistence in the file that exists to test isolation.
        dedupe=False,
        screen=False,
    )


def _context(settings, deps: RunnerDeps) -> _RunContext:
    client = httpx.AsyncClient()
    return _RunContext(
        deps=deps,
        settings=settings,
        client=client,
        robots=RobotsPolicy(client, user_agent=settings.source_user_agent, cache_ttl_s=60),
        breaker=InRunCircuitBreaker(threshold=settings.circuit_breaker_failures),
        limiter=NullRateLimiter(),
        run_id="01RUN",
    )


def _session(sources) -> FakeSession:
    return FakeSession(select_rows=[sources])


# --------------------------------------------------------------------------
# One broken source
# --------------------------------------------------------------------------


async def test_one_adapter_raising_does_not_abort_the_run(settings) -> None:
    sources = [make_source(1), make_source(2), make_source(3)]
    persister = RecordingPersister()
    deps = _deps(
        settings,
        adapters={
            1: FakeAdapter(postings=[make_posting(external_id="a")]),
            2: TransportError("ConnectError from boards-api.greenhouse.io/…"),
            3: FakeAdapter(postings=[make_posting(external_id="b")]),
        },
        persister=persister,
    )

    outcome = await run_discovery(_session(sources), "01RUN", deps=deps)

    assert [result.source_id for result in outcome.results] == [1, 2, 3]
    statuses = {result.source_id: result.status for result in outcome.results}
    assert statuses[1] is SourceStatus.OK
    assert statuses[2] is SourceStatus.ERROR
    assert statuses[3] is SourceStatus.OK
    # The healthy sources still persisted their postings.
    assert {call[0] for call in persister.calls} == {1, 2, 3}
    assert outcome.status is RunStatus.COMPLETED_WITH_ERRORS


async def test_every_source_failing_is_completed_with_errors_not_failed(settings) -> None:
    sources = [make_source(index) for index in (1, 2, 3, 4)]
    deps = _deps(
        settings,
        adapters={
            1: TransportError("transport"),
            2: SchemaDriftError("api.lever.co/v0/postings/{site} did not match"),
            3: UpstreamHttpError("HTTP 404", status_code=404),
            4: RobotsDenied("robots.txt disallows example.com/jobs"),
        },
    )

    outcome = await run_discovery(_session(sources), "01RUN", deps=deps)

    assert len(outcome.results) == 4
    assert all(result.status is not SourceStatus.OK for result in outcome.results)
    # Adapter failures never produce `failed`. That is invariant 5 expressed as
    # a state machine (§10.4).
    assert outcome.status is RunStatus.COMPLETED_WITH_ERRORS
    assert outcome.stats.sources_failed == 4
    assert outcome.stats.sources_total == 4


async def test_a_run_where_nothing_fails_is_completed(settings) -> None:
    sources = [make_source(1), make_source(2)]
    deps = _deps(
        settings,
        adapters={
            1: FakeAdapter(postings=[make_posting(external_id="a")]),
            2: FakeAdapter(postings=[]),
        },
    )

    outcome = await run_discovery(_session(sources), "01RUN", deps=deps)

    assert {result.status for result in outcome.results} == {
        SourceStatus.OK,
        SourceStatus.EMPTY,
    }
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.stats.sources_ok == 1
    assert outcome.stats.sources_empty == 1


# --------------------------------------------------------------------------
# The outer layer: a crash inside fetch_one itself
# --------------------------------------------------------------------------


async def test_a_crash_inside_fetch_one_is_recorded_not_raised(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exploding(_source, *, ctx):
        raise RuntimeError("the classifier itself broke")

    monkeypatch.setattr(runner_module, "_fetch_one", exploding)

    sources = [make_source(1), make_source(2)]
    deps = _deps(settings, adapters={1: FakeAdapter(), 2: FakeAdapter()})

    outcome = await run_discovery(_session(sources), "01RUN", deps=deps)

    assert len(outcome.results) == 2
    for result in outcome.results:
        assert result.status is SourceStatus.ERROR
        assert result.error_code == "adapter.unknown"
        # The exception's own text may quote an upstream body, so only the type
        # survives into a field that is rendered in the digest.
        assert result.error == "runner crashed: RuntimeError"
        assert "the classifier itself broke" not in (result.error or "")
    assert outcome.status is RunStatus.COMPLETED_WITH_ERRORS


async def test_fetch_one_never_raises_an_adapter_failure(settings) -> None:
    deps = _deps(settings, adapters={1: ValueError("something nobody classified")})
    ctx = _context(settings, deps)
    source = DueSource(id=1, company_id=1, adapter=AtsType.GREENHOUSE, config={})

    outcome = await fetch_one(source, ctx=ctx)

    assert outcome.result.status is SourceStatus.ERROR
    assert outcome.result.error_code == "adapter.unknown"


# --------------------------------------------------------------------------
# Cancellation is never swallowed
# --------------------------------------------------------------------------


async def test_cancelled_error_propagates_out_of_fetch_one(settings) -> None:
    # The whole-run budget must be able to stop the run; a task that eats its
    # own cancellation is a task that runs forever.
    deps = _deps(settings, adapters={1: asyncio.CancelledError()})
    ctx = _context(settings, deps)
    source = DueSource(id=1, company_id=1, adapter=AtsType.GREENHOUSE, config={})

    with pytest.raises(asyncio.CancelledError):
        await fetch_one(source, ctx=ctx)


async def test_cancelled_error_from_inside_fetch_propagates(settings) -> None:
    adapter = FakeAdapter(raises=asyncio.CancelledError())
    deps = _deps(settings, adapters={1: adapter})
    ctx = _context(settings, deps)
    source = DueSource(id=1, company_id=1, adapter=AtsType.GREENHOUSE, config={})

    with pytest.raises(asyncio.CancelledError):
        await fetch_one(source, ctx=ctx)


# --------------------------------------------------------------------------
# Partial ingestion is forbidden
# --------------------------------------------------------------------------


async def test_a_timed_out_source_discards_everything_it_yielded(settings) -> None:
    # A half-fetched board looks like "everything else closed" to the two-run
    # rule and would close live postings (§4.4).
    fast = settings.model_copy(update={"source_timeout_s": 1})
    persister = RecordingPersister()
    deps = _deps(
        fast,
        adapters={
            1: FakeAdapter(
                postings=[make_posting(external_id="a"), make_posting(external_id="b")],
                hang_after=30,
            )
        },
        persister=persister,
    )

    outcome = await run_discovery(_session([make_source(1)]), "01RUN", deps=deps)

    result = outcome.results[0]
    assert result.status is SourceStatus.TIMEOUT
    assert result.fetched == 0
    assert result.error_code == "adapter.timeout"
    # Nothing was handed to persistence, not even the two it had already read.
    assert persister.calls[0][1] == []


# --------------------------------------------------------------------------
# Status classification (§10.2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TransportError("x"), SourceStatus.ERROR),
        (SchemaDriftError("x"), SourceStatus.SCHEMA_ERROR),
        (UpstreamHttpError("x", status_code=404), SourceStatus.HTTP_ERROR),
        (UpstreamHttpError("x", status_code=503), SourceStatus.ERROR),
        (UpstreamHttpError("x", status_code=429), SourceStatus.RATE_LIMITED),
        (RateLimitTimeout("x"), SourceStatus.RATE_LIMITED),
        (RobotsDenied("x"), SourceStatus.ROBOTS_DENIED),
        (DeniedByPolicy("linkedin.com"), SourceStatus.DENIED_BY_POLICY),
        (KeyError("x"), SourceStatus.ERROR),
    ],
)
def test_failures_classify_to_the_documented_status(exc, expected) -> None:
    status, _error, _code = classify_failure(exc)
    assert status is expected


def test_an_unknown_exception_contributes_only_its_type() -> None:
    _status, error, code = classify_failure(KeyError("a response body fragment"))
    assert error == "unexpected KeyError"
    assert code == "adapter.unknown"
    assert "response body fragment" not in error


# --------------------------------------------------------------------------
# Refusals: the only paths that produce `failed`
# --------------------------------------------------------------------------


async def test_a_second_concurrent_run_is_refused_not_queued(settings) -> None:
    deps = _deps(settings, adapters={}, lock=FakeLock(acquired=False))

    with pytest.raises(RunAlreadyInFlight):
        await run_discovery(_session([]), "01RUN", deps=deps)


async def test_redis_being_unavailable_refuses_the_run(settings) -> None:
    lock = FakeLock(error=RedisConnectionError("redis is gone"))
    deps = _deps(settings, adapters={}, lock=lock)
    session = _session([])

    with pytest.raises(RunLockUnavailable):
        await run_discovery(session, "01RUN", deps=deps)

    # Recorded as a failed run rather than silently proceeding without a lock.
    assert session.added
    assert session.added[0].status is RunStatus.FAILED


async def test_a_run_with_no_due_sources_completes(settings) -> None:
    deps = _deps(settings, adapters={})

    outcome = await run_discovery(_session([]), "01RUN", deps=deps)

    assert outcome.results == []
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.stats.sources_total == 0


# --------------------------------------------------------------------------
# The durable circuit breaker (§4.8)
# --------------------------------------------------------------------------


async def _outcome(settings, status: SourceStatus, *, failures: int = 0) -> dict:
    """Record one outcome against a fake session and return the SET values."""
    session = FakeSession()
    source = make_source(7)
    source.consecutive_failures = failures
    session.gets[7] = source

    await record_source_outcome(
        session,
        source_id=7,
        result=SourceResult(
            source_id=7,
            company_id=1,
            adapter=AtsType.GREENHOUSE,
            describe="Greenhouse · board",
            status=status,
            error="something the operator can read",
        ),
        now=utcnow(),
        auto_disable_threshold=settings.auto_disable_threshold,
    )
    return dict(session.updates[0].compile().params)


@pytest.mark.parametrize("status", [SourceStatus.OK, SourceStatus.EMPTY])
async def test_success_clears_the_failure_history(settings, status) -> None:
    # A source that works today is not on probation for last week's outage.
    values = await _outcome(settings, status, failures=3)
    assert values["consecutive_failures"] == 0
    assert values["last_error"] is None
    assert values["last_status"] == status.value


async def test_a_failure_increments_the_counter(settings) -> None:
    values = await _outcome(settings, SourceStatus.HTTP_ERROR, failures=1)
    assert values["consecutive_failures"] == 2
    assert values["enabled"] is True
    assert values["last_status"] == "http_error"


async def test_the_threshold_auto_disables(settings) -> None:
    values = await _outcome(
        settings, SourceStatus.ERROR, failures=settings.auto_disable_threshold - 1
    )
    assert values["consecutive_failures"] == settings.auto_disable_threshold
    assert values["enabled"] is False
    assert values["last_status"] == "auto_disabled"


@pytest.mark.parametrize(
    "status", [SourceStatus.RATE_LIMITED, SourceStatus.CIRCUIT_OPEN, SourceStatus.DISABLED]
)
async def test_our_own_back_pressure_is_not_the_source_s_fault(settings, status) -> None:
    # Counting these would auto-disable healthy boards during a busy run.
    values = await _outcome(settings, status, failures=2)
    assert "consecutive_failures" not in values
    assert "enabled" not in values
    assert values["last_status"] == status.value


@pytest.mark.parametrize("status", [SourceStatus.ROBOTS_DENIED, SourceStatus.DENIED_BY_POLICY])
async def test_a_refusal_disables_immediately(settings, status) -> None:
    # Retrying a refusal four more times is four more requests we were told not
    # to make.
    values = await _outcome(settings, status, failures=0)
    assert values["enabled"] is False
    assert values["consecutive_failures"] == 1
    assert values["last_status"] == status.value


async def test_the_lock_is_released_even_when_the_run_fails(settings, monkeypatch) -> None:
    async def exploding(*_args, **_kwargs):
        raise RuntimeError("the runner itself broke")

    monkeypatch.setattr(runner_module, "_execute", exploding)
    lock = FakeLock()
    deps = _deps(settings, adapters={}, lock=lock)

    with pytest.raises(RuntimeError):
        await run_discovery(_session([]), "01RUN", deps=deps)

    assert lock.released is True


# ---------------------------------------------------------------------------
# --force relaxes the poll interval, and nothing else
# ---------------------------------------------------------------------------
#
# The interval is a scheduling rule, not a policy one. After fixing an adapter
# you want the whole registry re-fetched now; "wait until 08:00 tomorrow, or
# type out forty-three ids" is not a real choice. `enabled` and a blacklisted
# company are decisions, and a decision a re-run flag can bypass is not a
# decision — so force must not touch either.


def test_force_drops_the_due_clause_but_keeps_enabled_and_blacklist() -> None:
    """Asserted on the compiled SQL: the behaviour lives in the WHERE clause."""
    from sqlalchemy.dialects import postgresql

    from scout_careers.ingest import runner as runner_module

    captured: list[str] = []

    class _CapturingSession:
        async def execute(self, stmt: object) -> object:
            captured.append(
                str(stmt.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
            )

            class _Empty:
                def __iter__(self) -> object:
                    return iter(())

                def scalars(self) -> object:
                    return self

                def all(self) -> list[object]:
                    return []

            return _Empty()

    async def _run(force: bool) -> str:
        captured.clear()
        await runner_module.load_due_sources(
            _CapturingSession(),  # type: ignore[arg-type]
            None,
            force=force,
        )
        return captured[0]

    scheduled = asyncio.run(_run(force=False))
    forced = asyncio.run(_run(force=True))

    # The due-date comparison is the only thing force removes.
    assert "make_interval" in scheduled
    assert "make_interval" not in forced

    # Both keep the two conditions that are policy, not scheduling.
    for sql in (scheduled, forced):
        assert "enabled" in sql
        assert "status !=" in sql or "status IS DISTINCT FROM" in sql
        assert "deleted_at IS NULL" in sql


def test_force_is_threaded_from_run_discovery_to_the_query() -> None:
    """The wiring, not just the parameter.

    `--force` shipped once as a flag that reached `run_discovery`, was described
    in its docstring, and was never forwarded to the query it existed to change.
    The run then quietly selected only the two sources that had never run — which
    looks like a working command until you count the sources. mypy does not flag
    an unused parameter; ruff's ARG rule does, and this pins the wiring.
    """
    import inspect

    from scout_careers.ingest import runner as runner_module

    assert "force" in inspect.signature(runner_module.run_discovery).parameters
    assert "force" in inspect.signature(runner_module._execute).parameters
    assert "force" in inspect.signature(runner_module.load_due_sources).parameters

    # Each hop passes it on, rather than merely accepting it.
    assert "force=force" in inspect.getsource(runner_module.run_discovery)
    assert "load_due_sources(session, source_ids, force=force)" in inspect.getsource(
        runner_module._execute
    )
