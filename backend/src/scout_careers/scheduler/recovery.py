"""Startup reconciliation: close the runs a crash left open (SDD.md §6.5).

A process killed at 08:07, mid-discovery, leaves a ``run_log`` row saying
``running`` and no process behind it. Nothing ever closes that row on its own,
so without this it stays ``running`` forever: the dashboard shows a live run
that is not live, ``runs list`` shows a run with no ``finished_at``, and the
operator's first question every morning is whether yesterday actually finished.

What this does **not** do is as deliberate as what it does:

- **It does not resume the run.** A half-executed pipeline whose in-memory stage
  boundary is gone cannot be safely continued, and every stage is re-runnable
  anyway. The recovery action is one ``scout-careers run discovery``.
- **It does not delete the Redis run lock.** Deleting a lock this process does
  not own is the one thing a lock implementation must never do. The lock's TTL
  clears it — and configuration refuses to boot unless that TTL exceeds the
  whole-run budget, so the lock cannot outlive a run that is genuinely working
  (``common/config.py``).
- **It does not touch postings.** Each source commits in its own transaction and
  the not-seen counters only advance for sources that completed, so an
  interrupted run leaves nothing half-persisted and closes no live posting
  (``ingest/runner.py``).

It runs in the scheduler's startup path, **before** the scheduler starts, so a
crashed run can never be the reason a fresh one looks like a duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scout_careers.common.clock import utcnow
from scout_careers.common.logging import get_logger
from scout_careers.common.types import RunStatus
from scout_careers.db.models import RunLog

log = get_logger(__name__)

#: Written to ``run_log.error``. Carries the ``process_restart`` token SDD.md
#: §6.5 names, plus the sentence that saves the operator a search for it.
RESTART_ERROR: Final[str] = (
    "process_restart: the run was interrupted by a restart and is not resumed; "
    "re-run it with `scout-careers run discovery`"
)


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """What startup found and closed.

    Attributes:
        stranded_runs: How many ``running`` rows were closed as ``failed``.
        run_ids: Their ULIDs, so the operator can look one up.
    """

    stranded_runs: int = 0
    run_ids: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """True when the last shutdown left nothing behind."""
        return self.stranded_runs == 0


async def reconcile_on_startup(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    older_than_s: int = 0,
) -> ReconciliationReport:
    """Close every ``running`` run as ``failed``.

    Called once, at startup, before the scheduler starts. At that moment no run
    of ours is in flight — ``SCHEDULER_ENABLED`` is true on exactly one process
    (``CONFIGURATION.md`` §7) — so every ``running`` row is by definition the
    residue of a process that died. That is why the default cutoff is zero
    rather than the run budget: a run interrupted ten seconds before the crash is
    exactly as dead as one interrupted ten minutes before it, and a budget-shaped
    grace period would leave the recent ones ``running`` forever.

    Args:
        session: The session to reconcile in. Committed here.
        now: The clock; the current UTC time when omitted.
        older_than_s: Only close runs that started at least this long ago. Zero
            — everything — is the correct value at startup. It exists for the
            caller that one day reconciles *while* a run may be live.

    Returns:
        What was closed.
    """
    moment = now or utcnow()
    cutoff = moment - timedelta(seconds=older_than_s)

    rows = await session.execute(
        select(RunLog.id).where(RunLog.status == RunStatus.RUNNING, RunLog.started_at <= cutoff)
    )
    run_ids = tuple(str(run_id) for run_id in rows.scalars().all())

    if not run_ids:
        await session.commit()
        log.info("startup_reconciled", stranded_runs=0)
        return ReconciliationReport()

    await session.execute(
        update(RunLog)
        .where(RunLog.id.in_(run_ids))
        .values(status=RunStatus.FAILED, finished_at=moment, error=RESTART_ERROR)
    )
    await session.commit()

    log.warning("startup_reconciled", stranded_runs=len(run_ids), run_ids=list(run_ids))
    return ReconciliationReport(stranded_runs=len(run_ids), run_ids=run_ids)


__all__ = ["RESTART_ERROR", "ReconciliationReport", "reconcile_on_startup"]
