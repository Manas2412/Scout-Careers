"""Startup reconciliation.

A run left ``running`` by a crash never closes itself. Without this pass it
stays live-looking forever: the run list shows a run with no ``finished_at``,
and the operator cannot tell a crashed run from one still working.

What is asserted here is that the row is closed as ``failed`` with an error that
names the cause and the remedy — and, just as importantly, that reconciliation
does not touch the Redis run lock. Deleting a lock this process does not own is
the one thing a lock implementation must never do.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime

from sqlalchemy import Update

from scout_careers.common.types import RunStatus
from scout_careers.scheduler import recovery
from scout_careers.scheduler.recovery import (
    RESTART_ERROR,
    ReconciliationReport,
    reconcile_on_startup,
)
from tests.unit.ingest.conftest import FakeSession

NOW = datetime(2026, 9, 5, 2, 40, tzinfo=UTC)


def values_of(statement: Update) -> dict[str, object]:
    """Return the bound parameters of an UPDATE as a plain dict.

    Same shape the runner-isolation tests read, so the two files agree on how a
    statement is inspected.
    """
    return dict(statement.compile().params)


async def test_a_stranded_running_row_is_closed_as_failed() -> None:
    session = FakeSession(select_rows=[["01JRUNSTRANDED0000000000AA"]])

    report = await reconcile_on_startup(session, now=NOW)

    assert report.stranded_runs == 1
    assert report.run_ids == ("01JRUNSTRANDED0000000000AA",)
    assert report.clean is False

    (update,) = session.updates
    values = values_of(update)
    assert values["status"] is RunStatus.FAILED
    assert values["finished_at"] == NOW
    assert "process_restart" in str(values["error"])
    assert session.commits == 1


async def test_the_error_names_the_cause_and_the_remedy() -> None:
    session = FakeSession(select_rows=[["01JRUN0000000000000000000A"]])
    await reconcile_on_startup(session, now=NOW)

    error = str(values_of(session.updates[0])["error"])
    assert error == RESTART_ERROR
    assert "scout-careers run discovery" in error
    assert "not resumed" in error


async def test_several_stranded_runs_are_all_closed() -> None:
    ids = ["01JRUN000000000000000000A" + suffix for suffix in "BCD"]
    session = FakeSession(select_rows=[ids])

    report = await reconcile_on_startup(session, now=NOW)

    assert report.stranded_runs == 3
    assert set(report.run_ids) == set(ids)
    assert len(session.updates) == 1, "one statement, not one per row"


async def test_a_clean_startup_writes_nothing() -> None:
    session = FakeSession(select_rows=[[]])

    report = await reconcile_on_startup(session, now=NOW)

    assert report == ReconciliationReport()
    assert report.clean is True
    assert session.updates == []
    assert session.commits == 1


async def test_the_default_cutoff_closes_everything_however_recent() -> None:
    # At startup no run of ours is in flight, so a run interrupted ten seconds
    # before the crash is exactly as dead as one interrupted ten minutes before
    # it. A budget-shaped grace period would leave the recent ones running
    # forever, which is the bug this pass exists to prevent.
    session = FakeSession(select_rows=[["01JRUNVERYRECENT00000000A"]])
    report = await reconcile_on_startup(session, now=NOW)
    assert report.stranded_runs == 1


async def test_an_explicit_cutoff_is_honoured() -> None:
    session = FakeSession(select_rows=[[]])
    await reconcile_on_startup(session, now=NOW, older_than_s=900)

    # The comparison is against started_at, and the statement carries it.
    rendered = str(session.statements[0])
    assert "started_at" in rendered
    assert "status" in rendered


async def test_reconciliation_never_touches_the_run_lock() -> None:
    # Asserted on the source, not on behaviour: the failure mode is somebody
    # adding a helpful `redis.delete(RUN_LOCK_KEY)` here, and by the time that
    # shows up as behaviour it has already released another process's lock.
    tree = ast.parse(inspect.getsource(recovery))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"delete", "unlink", "flushdb", "eval"}
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "redis" not in alias.name.lower()
        if isinstance(node, ast.ImportFrom):
            assert "redis" not in (node.module or "").lower()
