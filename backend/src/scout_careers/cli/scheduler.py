"""``scout-careers scheduler …`` — start it, list it, fire a job now.

Three commands, one for each thing an operator actually does with a scheduler:
run it (that is the container's command), see when it next fires, and make it
fire now because they just fixed a board token and do not want to wait until
tomorrow morning.
"""

from __future__ import annotations

from typing import Annotated

import typer

from scout_careers.cli._async import run as run_async
from scout_careers.cli.output import dash, echo, echo_json, echo_table, error
from scout_careers.common.config import Settings, get_settings
from scout_careers.common.logging import configure_logging
from scout_careers.scheduler.app import (
    build_scheduler,
    job_infos,
    serve,
    shutdown,
    start,
)
from scout_careers.scheduler.jobs import DISCOVERY_JOB_ID, JOB_SPECS, run_discovery_job
from scout_careers.sources.registry import verify_registry

app = typer.Typer(no_args_is_help=True, help="The job scheduler.")

#: What ``scheduler trigger`` accepts. One job in Phase 1; the mapping exists so
#: adding a job is one entry rather than a new command.
TRIGGERABLE = {"discovery": run_discovery_job}


async def _serve(settings: Settings) -> int:
    report = await serve(settings)
    if report.stranded_runs:
        echo(
            f"Startup reconciliation closed {report.stranded_runs} interrupted run(s): "
            f"{', '.join(report.run_ids)}"
        )
    return 0


@app.command("start")
def start_command() -> None:
    """Run the scheduler in the foreground until SIGTERM. The container's command.

    Refuses to start when ``SCHEDULER_ENABLED`` is false, which is the default:
    two processes with the scheduler on means two 08:00 discovery runs, one of
    which is refused by the run lock and recorded as a refusal that looks like
    an incident.
    """
    settings = get_settings()
    configure_logging(settings)

    if not settings.scheduler_enabled:
        error(
            "SCHEDULER_ENABLED is false. Exactly one process may run the scheduler; "
            "set SCHEDULER_ENABLED=true on that one and leave it false everywhere else."
        )
        raise typer.Exit(code=1)

    # A missing adapter is a boot failure, not a 02:30 runtime error.
    verify_registry()

    echo(
        f"Scheduler starting: discovery at "
        f"{settings.discovery_cron_hour:02d}:{settings.discovery_cron_minute:02d} {settings.tz}, "
        f"misfire grace {settings.scheduler_misfire_grace_s}s, coalesce on."
    )
    code = run_async(_serve(settings))
    if code:
        raise typer.Exit(code=code)


async def _jobs(settings: Settings, as_json: bool) -> None:
    """Start the scheduler paused so next-fire times exist, then read them off."""
    scheduler = build_scheduler(settings)
    await start(scheduler, paused=True)
    try:
        infos = job_infos(scheduler)
    finally:
        await shutdown(scheduler, grace_s=0.0)

    if as_json:
        echo_json(
            [
                {
                    "id": info.id,
                    "name": info.name,
                    "trigger": info.trigger,
                    "next_run_utc": info.next_run_at,
                    "next_run_ist": info.next_run_ist,
                }
                for info in infos
            ]
        )
        return

    echo_table(
        ["job", "name", "next run (IST)", "next run (UTC)", "trigger"],
        [
            [
                info.id,
                info.name,
                dash(
                    info.next_run_ist.isoformat(timespec="seconds")
                    if info.next_run_ist is not None
                    else None
                ),
                dash(
                    info.next_run_at.isoformat(timespec="seconds")
                    if info.next_run_at is not None
                    else None
                ),
                info.trigger,
            ]
            for info in infos
        ],
    )


@app.command("jobs")
def jobs(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List the registered jobs and when each next fires.

    Reads the persisted job store, so it reports what the running scheduler will
    actually do rather than what this build's code would register. Starting it
    paused is what makes the next-fire times real without running anything.
    """
    settings = get_settings()
    configure_logging(settings)
    run_async(_jobs(settings, as_json))


async def _trigger(job: str) -> int:
    outcome = await TRIGGERABLE[job]()
    if outcome is None:
        error(
            "The run was refused. Either another discovery run is in flight — it is "
            "refused, not queued — or Redis is unavailable and no lock could be held."
        )
        return 1
    echo(f"Run {outcome.run_id}: {outcome.status.value}.")
    echo(
        f"  {outcome.stats.sources_total} source(s), "
        f"{outcome.stats.sources_failed} failed, "
        f"{outcome.stats.fetched} fetched, {outcome.stats.new} new."
    )
    return 0


@app.command("trigger")
def trigger(
    job: Annotated[str, typer.Argument(help=f"Which job to run now: {', '.join(TRIGGERABLE)}.")],
) -> None:
    """Fire a job immediately, respecting the run lock.

    Not a bypass. The job takes the same Redis lock the 08:00 run takes, so
    triggering while a run is in flight is refused rather than queued.
    """
    settings = get_settings()
    configure_logging(settings)

    if job not in TRIGGERABLE:
        error(f"No job named {job}. Known jobs: {', '.join(sorted(TRIGGERABLE))}.")
        raise typer.Exit(code=2)

    code = run_async(_trigger(job))
    if code:
        raise typer.Exit(code=code)


@app.command("show")
def show() -> None:
    """Print the schedule this build would register, without touching the database."""
    settings = get_settings()
    echo_table(
        ["job", "name", "trigger", "run_type"],
        [
            [
                spec.id,
                spec.name,
                str(spec.trigger_factory(settings)),
                spec.run_type,
            ]
            for spec in JOB_SPECS
        ],
    )
    echo()
    echo(
        f"coalesce=true, max_instances=1, misfire_grace_time={settings.scheduler_misfire_grace_s}s"
    )
    echo(
        f"shutdown grace {settings.scheduler_shutdown_grace_s}s; job store table "
        f"{settings.scheduler_jobstore_table}"
    )


__all__ = ["DISCOVERY_JOB_ID", "TRIGGERABLE", "app"]
