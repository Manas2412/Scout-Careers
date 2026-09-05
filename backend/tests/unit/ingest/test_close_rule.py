"""The two-run close rule, and the per-source scoping that makes it safe.

The failure this guards against is specific and expensive: a board that 500s for
two nights closing an employer's entire live listing, with the operator finding
out by wondering where forty roles went (SOURCE_ADAPTERS.md §10.5).
"""

from __future__ import annotations

import pytest
from sqlalchemy import Update

from scout_careers.common.types import SourceStatus
from scout_careers.ingest.close import (
    MissedPosting,
    advances_counter,
    close_missing,
    plan_close,
)
from tests.unit.ingest.conftest import RUN_START, FakeSession, Row

THRESHOLD = 2


# --------------------------------------------------------------------------
# Which outcomes let a counter move
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [SourceStatus.OK, SourceStatus.EMPTY])
def test_a_healthy_source_lets_its_postings_age(status: SourceStatus) -> None:
    assert advances_counter(status) is True


@pytest.mark.parametrize(
    "status",
    [
        SourceStatus.ERROR,
        SourceStatus.HTTP_ERROR,
        SourceStatus.SCHEMA_ERROR,
        SourceStatus.TIMEOUT,
        SourceStatus.RATE_LIMITED,
        SourceStatus.CIRCUIT_OPEN,
        SourceStatus.ROBOTS_DENIED,
        SourceStatus.DENIED_BY_POLICY,
        SourceStatus.DISABLED,
    ],
)
def test_every_other_outcome_freezes_the_counter(status: SourceStatus) -> None:
    # A failed fetch is evidence of nothing at all about whether a role is still
    # listed, so it must not be evidence that it is gone.
    assert advances_counter(status) is False


def test_empty_counts_as_seen_but_error_does_not() -> None:
    # An empty board is a healthy board with nothing open — that is real
    # evidence the roles are gone. A 500 is not.
    assert advances_counter(SourceStatus.EMPTY) is True
    assert advances_counter(SourceStatus.ERROR) is False


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def test_a_first_miss_only_advances_the_counter() -> None:
    plan = plan_close([MissedPosting(id="01A", missed_runs=0)], threshold=THRESHOLD)
    assert plan.advanced == ("01A",)
    assert plan.closed == ()


def test_a_second_consecutive_miss_closes() -> None:
    plan = plan_close([MissedPosting(id="01A", missed_runs=1)], threshold=THRESHOLD)
    assert plan.closed == ("01A",)
    assert plan.advanced == ()


def test_two_clean_misses_close_a_posting() -> None:
    first = plan_close([MissedPosting(id="01A", missed_runs=0)], threshold=THRESHOLD)
    assert first.closed == ()
    # The run above advanced it to 1; the next clean run sees that.
    second = plan_close([MissedPosting(id="01A", missed_runs=1)], threshold=THRESHOLD)
    assert second.closed == ("01A",)


def test_a_higher_threshold_takes_longer() -> None:
    plan = plan_close([MissedPosting(id="01A", missed_runs=1)], threshold=3)
    assert plan.advanced == ("01A",)
    assert plan.closed == ()


def test_nothing_missed_plans_nothing() -> None:
    assert plan_close([], threshold=THRESHOLD).is_empty is True


# --------------------------------------------------------------------------
# The applied rule
# --------------------------------------------------------------------------


async def test_a_flaky_source_freezes_its_own_postings() -> None:
    session = FakeSession(select_rows=[[Row(id="01A", missed_runs=1)]])

    closed = await close_missing(
        session,
        source_id=7,
        status=SourceStatus.HTTP_ERROR,
        seen_at=RUN_START,
        threshold=THRESHOLD,
    )

    assert closed == 0
    # Not even the SELECT runs: there is nothing to decide.
    assert session.statements == []


@pytest.mark.parametrize("status", [SourceStatus.OK, SourceStatus.EMPTY])
async def test_a_healthy_source_closes_its_missing_postings(status: SourceStatus) -> None:
    session = FakeSession(
        select_rows=[[Row(id="01A", missed_runs=1), Row(id="01B", missed_runs=0)]]
    )

    closed = await close_missing(
        session,
        source_id=7,
        status=status,
        seen_at=RUN_START,
        threshold=THRESHOLD,
    )

    assert closed == 1
    updates = [str(stmt) for stmt in session.statements if isinstance(stmt, Update)]
    assert len(updates) == 2
    # One statement advances only; the other advances and closes.
    assert sum("closed_at" in text for text in updates) == 1


async def test_a_source_whose_postings_were_all_seen_writes_nothing() -> None:
    session = FakeSession(select_rows=[[]])

    closed = await close_missing(
        session,
        source_id=7,
        status=SourceStatus.OK,
        seen_at=RUN_START,
        threshold=THRESHOLD,
    )

    assert closed == 0
    assert not [stmt for stmt in session.statements if isinstance(stmt, Update)]
