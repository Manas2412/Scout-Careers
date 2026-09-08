"""The scheduler: the trigger, the misfire policy, and the run lock.

Offline throughout. The job store is APScheduler's in-memory one, so no Postgres
is needed to assert that a missed window fires once; and the discovery job is
driven with fully injected runner dependencies, so no Redis and no network are
needed to assert that a second run is refused.

The load-bearing test in this file is
``test_a_missed_window_fires_once_not_five``. It runs the real APScheduler
against a real event loop with a trigger that is already overdue several times
over, and counts executions — because ``coalesce=True`` asserted as an attribute
proves that somebody set an attribute, and this proves the laptop that wakes at
09:00 does not launch five discovery runs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.triggers.interval import IntervalTrigger
from redis.exceptions import ConnectionError as RedisConnectionError

from scout_careers.common.clock import IST
from scout_careers.common.config import Settings
from scout_careers.ingest.runner import RunnerDeps
from scout_careers.scheduler.app import (
    COALESCE,
    MAX_INSTANCES,
    build_scheduler,
    discovery_trigger,
    job_infos,
    shutdown,
    start,
)
from scout_careers.scheduler.jobs import (
    DISCOVERY_JOB_ID,
    JOB_SPECS,
    RunTracker,
    run_discovery_job,
)
from tests.conftest import make_settings
from tests.unit.ingest.conftest import FakeLock, FakeSession


@pytest.fixture
def scheduler_settings() -> Settings:
    return make_settings(scheduler_enabled=True)


# --------------------------------------------------------------------------
# 1. The trigger resolves to 08:00 IST
# --------------------------------------------------------------------------


def test_the_discovery_trigger_fires_at_0800_ist(scheduler_settings: Settings) -> None:
    trigger = discovery_trigger(scheduler_settings)
    # From the middle of the night UTC, which is already morning in India.
    after = datetime(2026, 9, 5, 0, 5, tzinfo=UTC)
    fires_at = trigger.get_next_fire_time(None, after)

    assert fires_at is not None
    in_ist = fires_at.astimezone(IST)
    assert (in_ist.hour, in_ist.minute) == (8, 0)
    # And in UTC that is 02:30 — the point being that a UTC-scheduled "08:00"
    # would fire at 13:30 IST and look, to the operator, like a job that did
    # not fire at all.
    assert (fires_at.astimezone(UTC).hour, fires_at.astimezone(UTC).minute) == (2, 30)


def test_the_trigger_fires_once_a_day(scheduler_settings: Settings) -> None:
    trigger = discovery_trigger(scheduler_settings)
    first = trigger.get_next_fire_time(None, datetime(2026, 9, 5, 3, 0, tzinfo=UTC))
    assert first is not None
    second = trigger.get_next_fire_time(first, first + timedelta(seconds=1))
    assert second is not None
    assert second - first == timedelta(days=1)


def test_the_trigger_hour_and_minute_come_from_configuration() -> None:
    settings = make_settings(discovery_cron_hour=21, discovery_cron_minute=45)
    fires_at = discovery_trigger(settings).get_next_fire_time(
        None, datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
    )
    assert fires_at is not None
    in_ist = fires_at.astimezone(IST)
    assert (in_ist.hour, in_ist.minute) == (21, 45)


def test_the_trigger_uses_tz_rather_than_a_second_timezone_setting() -> None:
    # One timezone field, not two that can disagree and produce a run at a time
    # nobody chose.
    settings = make_settings(tz="UTC")
    fires_at = discovery_trigger(settings).get_next_fire_time(
        None, datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
    )
    assert fires_at is not None
    assert fires_at.astimezone(UTC).hour == 8


# --------------------------------------------------------------------------
# 2. Job defaults, and what coalesce actually does
# --------------------------------------------------------------------------


def test_the_job_defaults_are_the_documented_ones(scheduler_settings: Settings) -> None:
    scheduler = build_scheduler(scheduler_settings, jobstore=MemoryJobStore())
    defaults = scheduler._job_defaults

    assert defaults["coalesce"] is True
    assert defaults["max_instances"] == 1
    assert defaults["misfire_grace_time"] == scheduler_settings.scheduler_misfire_grace_s
    assert (COALESCE, MAX_INSTANCES) == (True, 1)


def test_exactly_one_job_is_registered_in_phase_one(scheduler_settings: Settings) -> None:
    scheduler = build_scheduler(scheduler_settings, jobstore=MemoryJobStore())
    assert [job.id for job in scheduler.get_jobs()] == [DISCOVERY_JOB_ID]
    assert len(JOB_SPECS) == 1


async def test_a_missed_window_fires_once_not_five(scheduler_settings: Settings) -> None:
    """A laptop asleep through several windows runs the job once when it wakes.

    Real APScheduler, real event loop. The trigger is a one-second interval whose
    next fire time is already ten seconds in the past, so the scheduler has ten
    elapsed windows waiting for it the instant it starts. Without coalescing it
    submits all ten.
    """
    calls: list[float] = []

    async def job() -> None:
        calls.append(asyncio.get_running_loop().time())

    scheduler = build_scheduler(scheduler_settings, jobstore=MemoryJobStore(), specs=())
    overdue = datetime.now(UTC) - timedelta(seconds=10)
    scheduler.add_job(
        func=job,
        trigger=IntervalTrigger(seconds=1, start_date=overdue),
        id="overdue",
        next_run_time=overdue,
    )

    await start(scheduler)
    try:
        await asyncio.sleep(0.3)
    finally:
        await shutdown(scheduler, tracker=RunTracker(), grace_s=1.0)

    assert len(calls) == 1, f"a missed window must coalesce, got {len(calls)} runs"


def test_job_infos_report_the_next_fire_time(scheduler_settings: Settings) -> None:
    scheduler = build_scheduler(scheduler_settings, jobstore=MemoryJobStore())
    infos = job_infos(scheduler)
    assert [info.id for info in infos] == [DISCOVERY_JOB_ID]
    assert infos[0].name == "Daily discovery run"


async def test_job_infos_carry_an_ist_rendering(scheduler_settings: Settings) -> None:
    scheduler = build_scheduler(scheduler_settings, jobstore=MemoryJobStore())
    await start(scheduler, paused=True)
    try:
        (info,) = job_infos(scheduler)
        assert info.next_run_at is not None
        assert info.next_run_ist is not None
        assert (info.next_run_ist.hour, info.next_run_ist.minute) == (8, 0)
    finally:
        await shutdown(scheduler, tracker=RunTracker(), grace_s=0.0)


# --------------------------------------------------------------------------
# 3. The run lock: refused, not queued
# --------------------------------------------------------------------------


def _deps(settings: Settings, *, lock: FakeLock) -> RunnerDeps:
    """Runner dependencies with no Redis, no Postgres and no network."""
    return RunnerDeps(
        settings=settings,
        redis=None,
        lock=lock,
        session_factory=None,
        dedupe=False,
        screen=False,
    )


async def test_a_second_concurrent_run_is_refused_not_queued(
    scheduler_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = FakeLock(acquired=False)
    session = FakeSession()
    monkeypatch.setattr("scout_careers.scheduler.jobs.session_scope", _session_scope_over(session))

    outcome = await run_discovery_job(_deps(scheduler_settings, lock=lock), tracker=RunTracker())

    assert outcome is None, "the job reports a refusal, it does not queue behind the holder"
    assert session.commits == 0, "and nothing was written"


async def test_redis_being_unavailable_refuses_the_run(
    scheduler_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not a degradation: without Redis there is no lock, no shared token bucket
    # and no robots cache, and a run that proceeds anyway is the failure mode
    # invariant 8 exists to prevent.
    lock = FakeLock(error=RedisConnectionError("redis is gone"))
    session = FakeSession()
    monkeypatch.setattr("scout_careers.scheduler.jobs.session_scope", _session_scope_over(session))

    outcome = await run_discovery_job(_deps(scheduler_settings, lock=lock), tracker=RunTracker())

    assert outcome is None


async def test_a_run_that_completes_is_reported(
    scheduler_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = FakeLock(acquired=True)
    session = FakeSession(select_rows=[[]])
    monkeypatch.setattr("scout_careers.scheduler.jobs.session_scope", _session_scope_over(session))

    outcome = await run_discovery_job(_deps(scheduler_settings, lock=lock), tracker=RunTracker())

    assert outcome is not None
    assert outcome.status.value == "completed"
    assert lock.released is True, "the lock is released even on the happy path"


def _session_scope_over(session: FakeSession):
    """Return a ``session_scope`` stand-in yielding one fake session."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope(_factory: object = None):
        yield session

    return _scope


# --------------------------------------------------------------------------
# 4. Graceful shutdown
# --------------------------------------------------------------------------


async def test_shutdown_waits_for_an_in_flight_job() -> None:
    tracker = RunTracker()
    finished = asyncio.Event()

    async def slow() -> None:
        tracker.track()
        await asyncio.sleep(0.05)
        finished.set()

    task = asyncio.create_task(slow())
    await asyncio.sleep(0)  # let it register

    scheduler = build_scheduler(make_settings(), jobstore=MemoryJobStore(), specs=())
    cancelled = await shutdown(scheduler, tracker=tracker, grace_s=2.0)

    assert cancelled == 0
    assert finished.is_set()
    await task


async def test_shutdown_cancels_what_outlives_the_grace_period() -> None:
    tracker = RunTracker()

    async def forever() -> None:
        tracker.track()
        await asyncio.sleep(60)

    task = asyncio.create_task(forever())
    await asyncio.sleep(0)

    scheduler = build_scheduler(make_settings(), jobstore=MemoryJobStore(), specs=())
    cancelled = await shutdown(scheduler, tracker=tracker, grace_s=0.01)

    assert cancelled == 1
    assert task.cancelled() or task.done()


async def test_the_tracker_releases_a_finished_task() -> None:
    tracker = RunTracker()

    async def quick() -> None:
        task = tracker.track()
        assert len(tracker) == 1
        tracker.release(task)

    await asyncio.create_task(quick())
    assert len(tracker) == 0
    assert await tracker.drain(timeout_s=0.0) == 0
