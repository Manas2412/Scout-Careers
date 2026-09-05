"""Building, starting and stopping the scheduler (SDD.md §6).

The four decisions this module makes, and why each is what it is:

**Postgres job store.** ``SQLAlchemyJobStore`` against the same database, in
``apscheduler_jobs``. It is the only durability property this system needs — a
restart must not lose the schedule — and it is the entire justification for
APScheduler over Celery (``ARCHITECTURE.md`` §4.1). The store is synchronous and
cannot speak asyncpg, so it uses ``settings.database_url_sync``: the same URL
over ``psycopg``, derived rather than configured so the two cannot drift apart.
The table is APScheduler's own and is deliberately absent from ``db/models.py``
and from Alembic.

**``timezone`` is IST; everything else is UTC.** The trigger is the only place a
local timezone appears. Storage and computation stay UTC throughout
(``ARCHITECTURE.md`` §8) — a UTC scheduler fires the "08:00" job at 13:30 IST and
looks, to the operator, exactly like a job that did not fire.

**``coalesce=True`` and ``misfire_grace_time`` are chosen, not defaulted.**

    ``coalesce=True`` — a laptop asleep from Friday to Monday has three elapsed
    08:00 windows. Without coalescing, APScheduler submits three runs the moment
    it wakes; two of them are immediately refused by the Redis lock, and the
    ``run_log`` fills with refusals that look like incidents. With it, the job
    runs once. Three missed discovery runs and one discovery run produce the
    same postings anyway — discovery is a full re-read of every board, not a
    delta — so the extra two would be pure cost.

    ``misfire_grace_time`` — 30 minutes by default
    (``SCHEDULER_MISFIRE_GRACE_S``). A host that reboots, or a laptop that wakes
    at 08:20, still gets its run, and the 08:15 digest is only slightly stale.
    Past the window the run is **skipped**, on purpose: silently running
    yesterday's schedule six hours late produces a digest whose content the
    operator has already acted on, and a run that competes with today's
    (SDD.md §6.4). The operator triggers it by hand instead.

    ``max_instances=1`` — belt to the run lock's braces. The lock is what
    actually prevents two writers; this prevents one job overlapping itself
    inside one process, which is cheaper to enforce here than to detect there.

**Shutdown is bounded.** SIGTERM stops the scheduler immediately so no new job
starts, then waits up to ``SCHEDULER_SHUTDOWN_GRACE_S`` for an in-flight run and
cancels what is left. The runner re-raises ``CancelledError`` everywhere rather
than swallowing it, so a cancelled run unwinds rather than continuing headless.
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from scout_careers.common.clock import to_ist
from scout_careers.common.config import Settings, get_settings
from scout_careers.common.logging import get_logger
from scout_careers.db.session import session_scope
from scout_careers.scheduler.jobs import (
    DISCOVERY_JOB_ID,
    JOB_SPECS,
    TRACKER,
    JobSpec,
    RunTracker,
)
from scout_careers.scheduler.recovery import ReconciliationReport, reconcile_on_startup

log = get_logger(__name__)

#: Never two of the same job at once, inside one process.
MAX_INSTANCES: Final[int] = 1

#: Missed fires of one job collapse into a single run.
COALESCE: Final[bool] = True


@dataclass(frozen=True, slots=True)
class JobInfo:
    """One row of ``scout-careers scheduler jobs``.

    Attributes:
        id: The job id.
        name: Its human name.
        trigger: The trigger, rendered.
        next_run_at: The next fire time in UTC, or ``None`` when the scheduler
            has not computed one — which is what a paused or never-started
            scheduler looks like.
    """

    id: str
    name: str
    trigger: str
    next_run_at: datetime | None = None

    @property
    def next_run_ist(self) -> datetime | None:
        """The next fire time in IST, which is the timezone the operator lives in."""
        return to_ist(self.next_run_at) if self.next_run_at is not None else None


def discovery_trigger(settings: Settings) -> CronTrigger:
    """Build the daily discovery trigger.

    Args:
        settings: Supplies ``discovery_cron_hour``, ``discovery_cron_minute``
            and ``tz``.

    Returns:
        A cron trigger that fires once a day at the configured local time. The
        timezone is ``settings.tz`` — the same field the rest of the system uses
        for display — rather than a second ``SCHEDULER_TIMEZONE`` setting that
        could be set to something else and produce a run at a time nobody chose.
    """
    return CronTrigger(
        hour=settings.discovery_cron_hour,
        minute=settings.discovery_cron_minute,
        timezone=ZoneInfo(settings.tz),
    )


def build_scheduler(
    settings: Settings,
    *,
    jobstore: Any | None = None,
    specs: tuple[JobSpec, ...] = JOB_SPECS,
) -> AsyncIOScheduler:
    """Build the scheduler with its job store, defaults and jobs.

    Args:
        settings: Configuration.
        jobstore: An alternative job store. Injected by the tests, which must
            not need a Postgres to assert that a missed window fires once.
        specs: The jobs to register.

    Returns:
        A configured, not-yet-started scheduler.
    """
    store = jobstore if jobstore is not None else _postgres_jobstore(settings)
    scheduler = AsyncIOScheduler(
        timezone=ZoneInfo(settings.tz),
        jobstores={"default": store},
        job_defaults={
            "coalesce": COALESCE,
            "max_instances": MAX_INSTANCES,
            "misfire_grace_time": settings.scheduler_misfire_grace_s,
        },
    )
    for spec in specs:
        scheduler.add_job(
            func=spec.func,
            trigger=spec.trigger_factory(settings),
            id=spec.id,
            name=spec.name,
            # The code is the source of truth for schedules: a changed cron
            # takes effect on deploy rather than requiring somebody to delete a
            # persisted row. Job definitions are never edited in the database.
            replace_existing=True,
            kwargs=dict(spec.kwargs),
        )
    return scheduler


def _postgres_jobstore(settings: Settings) -> SQLAlchemyJobStore:
    """Build the durable job store.

    Args:
        settings: Supplies the derived synchronous URL and the table name.

    Returns:
        The store. APScheduler creates the table itself on first use.
    """
    return SQLAlchemyJobStore(
        url=settings.database_url_sync,
        tablename=settings.scheduler_jobstore_table,
    )


def job_infos(scheduler: AsyncIOScheduler) -> list[JobInfo]:
    """Describe every registered job and when it next fires.

    Args:
        scheduler: A scheduler that has been started, possibly paused — a job's
            next fire time is computed when the scheduler processes it, so an
            un-started scheduler reports ``None`` for all of them.

    Returns:
        One entry per job, ordered by id.
    """
    infos = [
        JobInfo(
            id=job.id,
            name=job.name or job.id,
            trigger=str(job.trigger),
            next_run_at=getattr(job, "next_run_time", None),
        )
        for job in scheduler.get_jobs()
    ]
    return sorted(infos, key=lambda info: info.id)


async def start(scheduler: AsyncIOScheduler, *, paused: bool = False) -> None:
    """Start the scheduler.

    Args:
        scheduler: The scheduler to start.
        paused: Start without firing anything. Used by ``scheduler jobs``, which
            needs next-fire times computed but must not run a job as a
            side effect of listing them.
    """
    scheduler.start(paused=paused)
    log.info(
        "scheduler_started",
        paused=paused,
        jobs=[job.id for job in scheduler.get_jobs()],
    )


async def shutdown(
    scheduler: AsyncIOScheduler,
    *,
    tracker: RunTracker | None = None,
    grace_s: float = 60.0,
) -> int:
    """Stop the scheduler and drain what is in flight.

    ``wait=False`` on the scheduler itself is deliberate: APScheduler's own wait
    blocks the event loop this coroutine runs on, which would deadlock against
    the very task it is waiting for. The waiting is done here instead, against
    the tracker, with a bound.

    Args:
        scheduler: The running scheduler.
        tracker: Where in-flight jobs registered; the process-wide one when
            omitted.
        grace_s: How long to wait before cancelling.

    Returns:
        How many in-flight jobs had to be cancelled. Zero is the good outcome.
    """
    if scheduler.running:
        scheduler.shutdown(wait=False)
    cancelled = await (tracker or TRACKER).drain(timeout_s=grace_s)
    log.info("scheduler_stopped", cancelled=cancelled)
    return cancelled


async def serve(settings: Settings | None = None) -> ReconciliationReport:
    """Run the scheduler in the foreground until SIGTERM or SIGINT.

    The container's process. In order: reconcile the runs a crash left open,
    start the scheduler, wait for a signal, stop cleanly.

    Args:
        settings: Configuration; resolved from the environment when omitted.

    Returns:
        What startup reconciliation found, so the caller can report it.
    """
    resolved = settings or get_settings()

    async with session_scope() as session:
        report = await reconcile_on_startup(session)

    scheduler = build_scheduler(resolved)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(received, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - platform dependent
            # Windows, and any loop that will not take handlers. KeyboardInterrupt
            # still unwinds; SIGTERM is a container concern and containers are Linux.
            log.warning("scheduler_signal_handler_unavailable", signal=received.name)

    await start(scheduler)
    try:
        await stop.wait()
    finally:
        await shutdown(scheduler, grace_s=resolved.scheduler_shutdown_grace_s)
    return report


__all__ = [
    "COALESCE",
    "DISCOVERY_JOB_ID",
    "MAX_INSTANCES",
    "JobInfo",
    "build_scheduler",
    "discovery_trigger",
    "job_infos",
    "serve",
    "shutdown",
    "start",
]
