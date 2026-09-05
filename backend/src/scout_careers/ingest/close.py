"""The two-run close rule, scoped per source.

``closed_at`` is set on a posting not seen in two consecutive runs
(DATA_MODEL.md §4.1). The subtlety, and the reason this is its own module, is
SOURCE_ADAPTERS.md §10.5: a failed source must not count as a run in which its
postings "were not seen". A board that 500s for two nights would otherwise close
an employer's entire live listing, and the operator would find out by wondering
where forty roles went.

So the counter is per source, and it advances **only** on runs where that
source's own fetch returned ``ok`` or ``empty``. Any other status — timeout,
schema drift, rate limiting, a disabled source — freezes the counter for that
source's postings and for nobody else's. ``COUNTS_AS_SEEN_STATUSES`` in
``common/types.py`` is the single expression of that rule; this module is where
it is applied.

Being seen resets the counter and clears ``closed_at``, which is handled in
:mod:`~scout_careers.ingest.persist` as part of writing the row: a posting is
"seen" exactly when it was persisted this run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scout_careers.common.logging import get_logger
from scout_careers.common.types import COUNTS_AS_SEEN_STATUSES, SourceStatus
from scout_careers.db.models import JobPosting

log = get_logger(__name__)


def advances_counter(status: SourceStatus) -> bool:
    """Report whether a source's outcome lets its postings age.

    Args:
        status: The source's result status for this run.

    Returns:
        True only for ``ok`` and ``empty``. ``empty`` counts because a healthy
        board that lists nothing is evidence that the roles are gone; every
        failing status is evidence of nothing at all.
    """
    return status in COUNTS_AS_SEEN_STATUSES


@dataclass(frozen=True, slots=True)
class MissedPosting:
    """A stored posting this run did not see.

    Attributes:
        id: The posting's ULID.
        missed_runs: Its counter before this run.
    """

    id: str
    missed_runs: int


@dataclass(frozen=True, slots=True)
class ClosePlan:
    """The two buckets a missed posting can fall into.

    Attributes:
        advanced: Ids whose counter moves but which stay open.
        closed: Ids whose counter reaches the threshold and which are closed.
    """

    advanced: Sequence[str]
    closed: Sequence[str]

    @property
    def is_empty(self) -> bool:
        """Report whether the plan would write nothing."""
        return not self.advanced and not self.closed


def plan_close(missed: Iterable[MissedPosting], *, threshold: int) -> ClosePlan:
    """Split missed postings into "age it" and "close it".

    Pure, so the rule that decides when a role disappears from the operator's
    view is tested without a database.

    Args:
        missed: Open postings of one source that this run did not see.
        threshold: ``settings.ingest_close_after_missed_runs`` — 2, the two-run
            rule.

    Returns:
        The plan. Both buckets increment the counter; only the second sets
        ``closed_at``.
    """
    advanced: list[str] = []
    closed: list[str] = []
    for posting in missed:
        if posting.missed_runs + 1 >= threshold:
            closed.append(posting.id)
        else:
            advanced.append(posting.id)
    return ClosePlan(advanced=tuple(advanced), closed=tuple(closed))


async def load_missed(
    session: AsyncSession,
    *,
    source_id: int,
    seen_at: datetime,
) -> list[MissedPosting]:
    """Load a source's open postings that this run did not touch.

    Args:
        session: The session this source's work runs in.
        source_id: The source that just ran.
        seen_at: The clock value :mod:`~scout_careers.ingest.persist` stamped on
            everything it saw this run. Anything whose ``last_seen_at`` is older
            was not in this fetch. Keying on the timestamp rather than on a list
            of external ids keeps a 3,000-posting board from binding 3,000
            parameters to ask one question.

    Returns:
        The missed postings, with their current counters.
    """
    rows = await session.execute(
        select(JobPosting.id, JobPosting.missed_runs).where(
            JobPosting.source_id == source_id,
            JobPosting.closed_at.is_(None),
            JobPosting.last_seen_at < seen_at,
        )
    )
    return [MissedPosting(id=row.id, missed_runs=row.missed_runs) for row in rows.all()]


async def apply_close(
    session: AsyncSession,
    plan: ClosePlan,
    *,
    now: datetime,
) -> None:
    """Write a close plan.

    Args:
        session: The session this source's work runs in.
        plan: The plan from :func:`plan_close`.
        now: The timestamp written to ``closed_at``.
    """
    if plan.advanced:
        await session.execute(
            update(JobPosting)
            .where(JobPosting.id.in_(plan.advanced))
            .values(missed_runs=JobPosting.missed_runs + 1)
        )
    if plan.closed:
        await session.execute(
            update(JobPosting)
            .where(JobPosting.id.in_(plan.closed))
            .values(missed_runs=JobPosting.missed_runs + 1, closed_at=now)
        )


async def close_missing(
    session: AsyncSession,
    *,
    source_id: int,
    status: SourceStatus,
    seen_at: datetime,
    threshold: int,
) -> int:
    """Apply the close rule for one source, if its outcome permits.

    Args:
        session: The session this source's work runs in.
        source_id: The source that just ran.
        status: Its outcome. Anything but ``ok`` or ``empty`` returns
            immediately, leaving every counter exactly where it was.
        seen_at: The clock value the persist step stamped on what it saw.
        threshold: ``settings.ingest_close_after_missed_runs``.

    Returns:
        The number of postings closed by this call, for ``run_log.stats``.
    """
    if not advances_counter(status):
        log.debug("close_rule_frozen", source_id=source_id, status=status.value)
        return 0

    missed = await load_missed(session, source_id=source_id, seen_at=seen_at)
    plan = plan_close(missed, threshold=threshold)
    if plan.is_empty:
        return 0

    await apply_close(session, plan, now=seen_at)
    log.info(
        "close_rule_applied",
        source_id=source_id,
        advanced=len(plan.advanced),
        closed=len(plan.closed),
    )
    return len(plan.closed)


__all__ = [
    "ClosePlan",
    "MissedPosting",
    "advances_counter",
    "apply_close",
    "close_missing",
    "load_missed",
    "plan_close",
]
