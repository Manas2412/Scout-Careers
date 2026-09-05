"""The job functions, and the register of what is in flight.

One job in Phase 1. Its body is deliberately thin: it builds the runner's
dependencies, calls ``run_discovery``, and turns the two refusal exceptions into
log lines. It does **not** re-implement the Redis run lock — that lives in
``ingest/runner.py``, where the manual ``scout-careers run discovery`` path also
passes through it, and two implementations of one lock is two chances to hold it
differently.

The job function is module-level and takes no arguments, because APScheduler's
SQLAlchemy job store persists a job as a textual reference plus pickled
arguments. A tracker or a settings object passed as a keyword would have to be
picklable and would then be *stale* on the far side of a restart, which is the
opposite of what a durable job store is for. Test seams are optional parameters
with defaults; the scheduler never passes them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from scout_careers.common.config import Settings
from scout_careers.common.ids import new_ulid
from scout_careers.common.logging import get_logger
from scout_careers.db.session import session_scope
from scout_careers.ingest.runner import (
    RunAlreadyInFlight,
    RunLockUnavailable,
    RunnerDeps,
    RunOutcome,
    run_discovery,
)

log = get_logger(__name__)

#: The one job Phase 1 registers.
DISCOVERY_JOB_ID: Final[str] = "discovery_daily"


class RunTracker:
    """The set of job invocations currently in flight.

    Exists so that SIGTERM can wait for a run rather than severing it mid-write.
    APScheduler's ``AsyncIOScheduler`` runs a coroutine job as an ``asyncio``
    task but does not hand the task back, so each job registers its own task
    here on entry and removes it on exit.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def __len__(self) -> int:
        """How many invocations are in flight right now."""
        return len(self._tasks)

    def track(self) -> asyncio.Task[Any] | None:
        """Register the calling task, if there is one.

        Returns:
            The task, or ``None`` when called outside a task — which happens in
            a direct unit-test call and is not an error.
        """
        try:
            task = asyncio.current_task()
        except RuntimeError:  # pragma: no cover - no running loop
            return None
        if task is not None:
            self._tasks.add(task)
        return task

    def release(self, task: asyncio.Task[Any] | None) -> None:
        """Deregister a task that has finished."""
        if task is not None:
            self._tasks.discard(task)

    async def drain(self, *, timeout_s: float) -> int:
        """Wait for in-flight jobs, then cancel whatever is left.

        Bounded on purpose. An unbounded wait turns "stop the container" into
        "the container never stops", and the orchestrator's own SIGKILL arrives
        anyway — at a moment we did not choose, in the middle of a write we did
        not get to finish.

        Args:
            timeout_s: How long to wait before cancelling.

        Returns:
            How many tasks had to be cancelled. Zero is the good outcome.
        """
        pending = {task for task in self._tasks if not task.done()}
        if not pending:
            return 0
        log.info("scheduler_draining", in_flight=len(pending), timeout_s=timeout_s)
        _, still_running = await asyncio.wait(pending, timeout=timeout_s)
        for task in still_running:
            task.cancel()
        if still_running:
            # Give the cancellations a chance to unwind; the runner re-raises
            # CancelledError everywhere rather than swallowing it, so this is
            # bounded by the tasks' own cleanup.
            await asyncio.wait(still_running, timeout=timeout_s)
            log.warning("scheduler_cancelled_in_flight", cancelled=len(still_running))
        return len(still_running)


#: The process-wide tracker the registered jobs report to.
TRACKER: Final[RunTracker] = RunTracker()


@dataclass(frozen=True, slots=True)
class JobSpec:
    """One registered job.

    Attributes:
        id: APScheduler's job id, and what ``scheduler trigger`` takes.
        name: The human name, used in ``scheduler jobs``.
        func: The coroutine the scheduler calls. Module-level and argument-free.
        trigger_factory: Builds the trigger from settings, so the schedule is
            configuration and the job is code.
        run_type: What lands in ``run_log.run_type``.
    """

    id: str
    name: str
    func: Callable[[], Awaitable[Any]]
    trigger_factory: Callable[[Settings], Any]
    run_type: str = "discovery"
    kwargs: dict[str, Any] = field(default_factory=dict)


async def run_discovery_job(
    deps: RunnerDeps | None = None,
    *,
    tracker: RunTracker | None = None,
) -> RunOutcome | None:
    """Run stage ① for every due source. The 08:00 job.

    Args:
        deps: Injected runner dependencies. The production set when omitted;
            the scheduler never passes this, the tests always do.
        tracker: Where the invocation registers itself. The process-wide
            tracker when omitted.

    Returns:
        The run outcome, or ``None`` when the run was refused. A refusal is not
        an exception here: APScheduler would log it as a job error and, on a bad
        day, treat the job as unhealthy — but "the operator triggered a run by
        hand at 07:59" is a correct outcome, not a fault.
    """
    registry = tracker or TRACKER
    task = registry.track()
    resolved = deps or RunnerDeps.build()
    run_id = new_ulid()
    owns_deps = deps is None

    try:
        async with session_scope() as session:
            outcome = await run_discovery(session, run_id, deps=resolved)
    except RunAlreadyInFlight:
        # Refused, not queued: two runs writing the same (source_id,
        # external_id) rows would race the close rule against itself.
        log.warning("scheduled_run_refused", job_id=DISCOVERY_JOB_ID, reason="already_in_flight")
        return None
    except RunLockUnavailable:
        # Not a degradation. Without Redis there is no lock, no shared token
        # bucket and no robots cache, and a run that proceeds anyway is exactly
        # the failure mode invariant 8 exists to prevent.
        log.error("scheduled_run_refused", job_id=DISCOVERY_JOB_ID, reason="lock_unavailable")
        return None
    finally:
        if owns_deps:
            await resolved.aclose()
        registry.release(task)

    log.info(
        "scheduled_run_finished",
        job_id=DISCOVERY_JOB_ID,
        run_id=outcome.run_id,
        status=outcome.status.value,
    )
    return outcome


def _discovery_trigger(settings: Settings) -> Any:
    """Build the discovery cron trigger. Indirected so ``app`` owns the trigger shape."""
    from scout_careers.scheduler.app import discovery_trigger

    return discovery_trigger(settings)


#: Every job this build registers. The count is asserted by the tests, so a job
#: that quietly stops being scheduled is a failure rather than a silence.
JOB_SPECS: Final[tuple[JobSpec, ...]] = (
    JobSpec(
        id=DISCOVERY_JOB_ID,
        name="Daily discovery run",
        func=run_discovery_job,
        trigger_factory=_discovery_trigger,
        run_type="discovery",
    ),
)


__all__ = ["DISCOVERY_JOB_ID", "JOB_SPECS", "TRACKER", "JobSpec", "RunTracker", "run_discovery_job"]
