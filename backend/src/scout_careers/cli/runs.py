"""``scout-careers run discovery`` and ``scout-careers runs …``.

The per-source results table is the point of these commands. A 320-source system
has a board changing most weeks (SOURCE_ADAPTERS.md §10.4), and the operator has
to be able to see *which* one without opening a log aggregator.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import typer
from sqlalchemy import select

from scout_careers.cli.output import dash, echo, echo_json, echo_table, error
from scout_careers.common.config import get_settings
from scout_careers.common.ids import new_ulid
from scout_careers.common.logging import configure_logging
from scout_careers.common.types import SourceStatus
from scout_careers.db.models import RunLog
from scout_careers.db.session import session_scope
from scout_careers.ingest.runner import (
    RunAlreadyInFlight,
    RunLockUnavailable,
    RunnerDeps,
    load_due_sources,
    run_discovery,
)
from scout_careers.sources.base import SourceResult

run_app = typer.Typer(no_args_is_help=True, help="Trigger a pipeline run.")
runs_app = typer.Typer(no_args_is_help=True, help="Inspect past runs.")

#: What a source result contributes to the summary table.
_RESULT_COLUMNS = (
    "source",
    "adapter",
    "status",
    "fetched",
    "new",
    "upd",
    "same",
    "ms",
    "error",
)


def _result_row(result: SourceResult) -> list[str]:
    return [
        str(result.source_id),
        result.adapter.value,
        result.status.value,
        str(result.fetched),
        str(result.new),
        str(result.updated),
        str(result.unchanged),
        str(result.duration_ms),
        dash(result.error),
    ]


def _progress(result: SourceResult) -> None:
    """Print one line per source as it finishes."""
    marker = "ok " if result.status in (SourceStatus.OK, SourceStatus.EMPTY) else "!! "
    echo(
        f"  {marker}{result.describe} — {result.status.value}, "
        f"{result.fetched} fetched, {result.new} new"
        + (f" — {result.error}" if result.error else "")
    )


async def _dry_run(source_ids: list[int] | None, as_json: bool) -> None:
    async with session_scope() as session:
        due = await load_due_sources(session, source_ids)
    if as_json:
        echo_json(
            [
                {
                    "source_id": source.id,
                    "company_id": source.company_id,
                    "adapter": source.adapter.value,
                    "config": source.config,
                    "poll_interval_minutes": source.poll_interval_minutes,
                }
                for source in due
            ]
        )
        return
    echo(f"{len(due)} source(s) would run:")
    echo_table(
        ["source", "company", "adapter", "config", "every"],
        [
            [
                str(source.id),
                str(source.company_id),
                source.adapter.value,
                ", ".join(f"{key}={value}" for key, value in sorted(source.config.items())),
                f"{source.poll_interval_minutes}m",
            ]
            for source in due
        ],
    )


async def _discovery(source_ids: list[int] | None, as_json: bool) -> int:
    settings = get_settings()
    deps = RunnerDeps.build(settings)
    deps.on_result = None if as_json else _progress
    run_id = new_ulid()

    if not as_json:
        echo(f"Run {run_id} starting.")

    try:
        async with session_scope() as session:
            outcome = await run_discovery(session, run_id, source_ids, deps=deps)
    except RunAlreadyInFlight:
        error("A discovery run is already in flight. It was refused, not queued.")
        return 1
    except RunLockUnavailable:
        error("Redis is unavailable, so the run lock could not be held. The run was refused.")
        return 1
    finally:
        await deps.aclose()

    if as_json:
        echo_json(
            {
                "run_id": outcome.run_id,
                "status": outcome.status.value,
                "stats": outcome.stats.model_dump(mode="json"),
                "source_results": [result.model_dump(mode="json") for result in outcome.results],
            }
        )
    else:
        echo()
        echo_table(list(_RESULT_COLUMNS), [_result_row(result) for result in outcome.results])
        echo()
        echo_table(["stat", "value"], _stats_rows(outcome.stats.model_dump(mode="json")))
        echo()
        echo(f"Run {outcome.run_id}: {outcome.status.value}.")
    return 0 if outcome.status.value != "failed" else 1


def _stats_rows(stats: dict[str, Any]) -> list[list[str]]:
    return [[key, str(value)] for key, value in sorted(stats.items())]


@run_app.command("discovery")
def discovery(
    source_ids: Annotated[
        list[int] | None,
        typer.Option("--source-id", help="Restrict to these sources. Repeatable."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what would run and stop.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Run stages ① DISCOVER, ② NORMALISE and ③ DEDUPE for every due source.

    Naming sources with ``--source-id`` ignores their poll interval — that is
    the point of re-running a subset after fixing a board token — but never
    overrides ``enabled`` or a blacklisted company.
    """
    configure_logging(get_settings())
    if dry_run:
        asyncio.run(_dry_run(source_ids, as_json))
        return
    code = asyncio.run(_discovery(source_ids, as_json))
    if code:
        raise typer.Exit(code=code)


async def _list_runs(limit: int, as_json: bool) -> None:
    async with session_scope() as session:
        result = await session.execute(
            select(RunLog).order_by(RunLog.started_at.desc()).limit(limit)
        )
        runs = list(result.scalars().all())

    if as_json:
        echo_json([_run_payload(run, include_results=False) for run in runs])
        return
    echo_table(
        ["id", "type", "status", "started", "finished", "sources", "new"],
        [
            [
                run.id,
                run.run_type,
                run.status.value,
                run.started_at.isoformat(timespec="seconds"),
                run.finished_at.isoformat(timespec="seconds") if run.finished_at else "—",
                str(run.stats.get("sources_total", "—")),
                str(run.stats.get("new", "—")),
            ]
            for run in runs
        ],
    )


def _run_payload(run: RunLog, *, include_results: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": run.id,
        "run_type": run.run_type,
        "status": run.status.value,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "stats": run.stats,
        "error": run.error,
    }
    if include_results:
        payload["source_results"] = run.source_results
    return payload


async def _show_run(run_id: str, as_json: bool) -> int:
    async with session_scope() as session:
        run = await session.get(RunLog, run_id)

    if run is None:
        error(f"No run {run_id}.")
        return 1

    if as_json:
        echo_json(_run_payload(run, include_results=True))
        return 0

    echo_table(
        ["field", "value"],
        [
            ["id", run.id],
            ["type", run.run_type],
            ["status", run.status.value],
            ["started", run.started_at.isoformat(timespec="seconds")],
            ["finished", run.finished_at.isoformat(timespec="seconds") if run.finished_at else "—"],
            ["error", dash(run.error)],
        ],
    )
    echo()
    echo_table(["stat", "value"], _stats_rows(run.stats))
    echo()
    echo_table(
        list(_RESULT_COLUMNS),
        [
            [
                str(entry.get("source_id", "—")),
                str(entry.get("adapter", "—")),
                str(entry.get("status", "—")),
                str(entry.get("fetched", 0)),
                str(entry.get("new", 0)),
                str(entry.get("updated", 0)),
                str(entry.get("unchanged", 0)),
                str(entry.get("duration_ms", 0)),
                dash(entry.get("error")),
            ]
            for entry in run.source_results
        ],
    )
    return 0


@runs_app.command("list")
def list_runs(
    limit: Annotated[int, typer.Option(help="How many runs to show.")] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List recent runs, newest first."""
    asyncio.run(_list_runs(limit, as_json))


@runs_app.command("show")
def show_run(
    run_id: Annotated[str, typer.Argument(help="The run's ULID.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show one run's stats and its per-source results."""
    code = asyncio.run(_show_run(run_id, as_json))
    if code:
        raise typer.Exit(code=code)


__all__ = ["run_app", "runs_app"]
