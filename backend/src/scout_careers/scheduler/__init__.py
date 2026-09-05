"""The scheduler: one durable job, one lock, one clean shutdown.

Phase 1 registers exactly one job — discovery at 08:00 Asia/Kolkata. The other
six jobs ``CONFIGURATION.md`` §7 names belong to the phases that build the
stages they drive; registering an empty ``digest`` job now would put a green
tick next to something that does nothing.

Three properties are the whole reason this package exists rather than a cron
line:

- **The schedule survives a restart**, because APScheduler's job store is
  Postgres. That single durability property is the entire justification for
  APScheduler over Celery (``ARCHITECTURE.md`` §4.1).
- **A missed window does not become a thundering herd.** ``coalesce=True`` plus
  a bounded ``misfire_grace_time`` mean a laptop that wakes at 09:00 runs
  discovery zero times, and one that wakes at 08:20 runs it once — never five.
- **A second run is refused, not queued.** The Redis lock lives in
  ``ingest/runner.py`` and is not re-implemented here; the scheduler's job calls
  the runner and reports the refusal.
"""

from __future__ import annotations

from scout_careers.scheduler.app import (
    DISCOVERY_JOB_ID,
    JobInfo,
    build_scheduler,
    discovery_trigger,
    job_infos,
    serve,
    shutdown,
)
from scout_careers.scheduler.jobs import JOB_SPECS, JobSpec, RunTracker, run_discovery_job
from scout_careers.scheduler.recovery import ReconciliationReport, reconcile_on_startup

__all__ = [
    "DISCOVERY_JOB_ID",
    "JOB_SPECS",
    "JobInfo",
    "JobSpec",
    "ReconciliationReport",
    "RunTracker",
    "build_scheduler",
    "discovery_trigger",
    "job_infos",
    "reconcile_on_startup",
    "run_discovery_job",
    "serve",
    "shutdown",
]
