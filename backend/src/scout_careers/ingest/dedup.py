"""Stage ③: collapsing the same role seen through two different sources.

The key is ``(company_id, normalise_title(title), location_city)``
(DATA_MODEL.md §4.1), qualified by :func:`same_role`: two records sharing that
key are collapsed only when they come from **different sources**, or from the
same source with the **same ``content_hash``**. The qualifier is not a detail —
without it 685 of 722 collapsed records on the first live registry were distinct
jobs that happened to share a title and a city, hidden from the operator by a key
that cannot tell a duplicate listing from a duplicate title.

The winner is the record whose source has the higher ``fidelity_rank``
(SOURCE_ADAPTERS.md §8); equal ranks — ``ashby`` and ``greenhouse`` both sit at
90 — break on ``first_seen_at`` **ascending**, so the record we saw first wins
and collapsing does not thrash when both boards list the same role.

Three properties are load-bearing and each has a test:

- **Losers are not deleted.** They are marked ``filtered_out = True`` with
  ``filter_reason = "superseded_by:<winner id>"``. The row is evidence: it is
  how the operator answers "did this ever appear on their own board, or only in
  a mail alert", and deleting it destroys that answer.
- **``closed_at`` is never touched.** ``closed_at`` belongs to the two-run close
  rule alone (:mod:`~scout_careers.ingest.close`). Supersession is a statement
  about which of two records to read; closure is a statement about whether the
  role is still listed. Conflating them makes a live role look dead the moment a
  better source finds it.
- **Supersession is reversible.** A row superseded last week whose winner has
  since disappeared wins its own group today and the flag is cleared. Only
  reasons carrying the ``superseded_by:`` prefix are ever cleared — a row
  filtered out for any other reason is left exactly as it was found.

This runs once per run, after every source has been persisted. Running it
per-source would let the source order decide the winner.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scout_careers.common.errors import AdapterConfigError
from scout_careers.common.logging import get_logger
from scout_careers.common.types import AtsType
from scout_careers.db.models import JobPosting, Source
from scout_careers.sources.normalise import normalise_title
from scout_careers.sources.registry import get_adapter

log = get_logger(__name__)

#: The one prefix. A second spelling of "this was superseded" would make the
#: reason column unqueryable, and the un-supersede path below keys on it.
SUPERSEDED_PREFIX = "superseded_by:"

#: SOURCE_ADAPTERS.md §8, verbatim. Rank lives on the adapter as a ``ClassVar``
#: and :func:`fidelity_rank` prefers it; this table covers the two cases where
#: there is no class to ask — ``manual``, which has no adapter by design, and the
#: eight adapters whose modules arrive in later phases but whose rows can already
#: exist by CSV import.
FIDELITY_RANKS: Mapping[AtsType, int] = {
    AtsType.MANUAL: 95,
    AtsType.ASHBY: 90,
    AtsType.GREENHOUSE: 90,
    AtsType.LEVER: 88,
    AtsType.WORKDAY: 85,
    AtsType.SMARTRECRUITERS: 82,
    AtsType.WORKABLE: 80,
    AtsType.RECRUITEE: 78,
    AtsType.GOOGLE: 75,
    AtsType.MICROSOFT: 74,
    AtsType.AMAZON: 72,
    AtsType.MAIL_ALERT: 20,
}


def fidelity_rank(adapter: AtsType) -> int:
    """Return the fidelity rank for an adapter type.

    Args:
        adapter: The ``source.adapter`` value.

    Returns:
        The registered adapter's ``fidelity_rank`` classvar where a class
        exists, otherwise the §8 table. Asking the class first is what keeps the
        documented statement true — rank is a property of the adapter, not a
        configurable value — while still ranking a ``manual`` row, which has no
        class by design.
    """
    try:
        return get_adapter(adapter).fidelity_rank
    except AdapterConfigError:
        return FIDELITY_RANKS.get(adapter, 0)


@dataclass(frozen=True, slots=True)
class DedupCandidate:
    """One posting as the collapse decision sees it.

    Attributes:
        id: The posting's ULID.
        company_id: Part of the key.
        title: The original title; the key uses ``normalise_title`` of it.
        location_city: Part of the key, canonicalised by the adapter.
        adapter: The owning source's adapter, which supplies the rank.
        source_id: The owning source. Two records from the *same* source are
            not cross-source duplicates by definition, so the key alone cannot
            decide them — see :func:`same_role`.
        content_hash: The hash of ``description_text``. The only evidence
            available for whether two same-source postings are one role listed
            twice or two roles with the same title.
        first_seen_at: The tie-break, ascending.
        filtered_out: Its current flag.
        filter_reason: Its current reason, which decides whether the flag is
            ours to clear.
    """

    id: str
    company_id: int
    title: str
    location_city: str | None
    adapter: AtsType
    source_id: int
    content_hash: str
    first_seen_at: datetime
    filtered_out: bool = False
    filter_reason: str | None = None

    @property
    def key(self) -> tuple[int, str, str | None]:
        """Return the cross-source dedup key."""
        return (self.company_id, normalise_title(self.title), self.location_city)

    @property
    def superseded(self) -> bool:
        """Report whether this row currently carries a supersession flag."""
        return self.filtered_out and (self.filter_reason or "").startswith(SUPERSEDED_PREFIX)


@dataclass(frozen=True, slots=True)
class DedupPlan:
    """The writes a collapse pass wants to make.

    Attributes:
        supersede: Loser id → winner id, for rows not already pointing there.
        restore: Ids whose supersession is no longer true.
    """

    supersede: Mapping[str, str]
    restore: Sequence[str]

    @property
    def is_empty(self) -> bool:
        """Report whether the plan would write nothing."""
        return not self.supersede and not self.restore


def same_role(candidate: DedupCandidate, winner: DedupCandidate) -> bool:
    """Report whether two records sharing the dedup key are really one role.

    Args:
        candidate: The record that would be superseded.
        winner: The record that would win.

    Returns:
        True when collapsing them is safe.

    ``(company, title, city)`` is the right key **across** sources: the same
    role on a company's Greenhouse board and in a LinkedIn alert has no shared
    identifier and no comparable text, so a title-and-place match is the only
    evidence there is.

    Within one source it is not enough, and the first live registry proved it:
    of 722 collapsed records, **685 had different description text**. Databricks
    lists 871 roles, and three separate "Software Engineer" openings in San
    Francisco are three jobs, not one posted three times. Nine per cent of every
    posting held was being hidden from the operator by a key that could not tell
    a duplicate listing from a duplicate title.

    Inside one source the description settles it, because the same board renders
    the same text every time: identical hash is one listing seen twice, and a
    different hash is a different job. Across sources the hashes never match —
    different ATSs render different text for the same role — which is exactly
    why the comparison is scoped to one source rather than applied everywhere.
    """
    if candidate.source_id != winner.source_id:
        return True
    return candidate.content_hash == winner.content_hash


def _precedence(candidate: DedupCandidate) -> tuple[int, float, str]:
    """Return the sort key that picks a winner: rank desc, first_seen asc, id asc.

    The id is a final tie-break so that two records created in the same
    microsecond collapse deterministically rather than alternating between runs.
    """
    return (-fidelity_rank(candidate.adapter), candidate.first_seen_at.timestamp(), candidate.id)


def plan_collapse(candidates: Iterable[DedupCandidate]) -> DedupPlan:
    """Decide which records to supersede and which to un-supersede.

    Pure, so the ranking rules are tested offline against hand-built rows.

    Args:
        candidates: Every open posting in scope, from any source.

    Returns:
        The plan. A group of one produces no supersession, and un-supersedes its
        single member if it is still carrying a stale flag.
    """
    groups: dict[tuple[int, str, str | None], list[DedupCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.key, []).append(candidate)

    supersede: dict[str, str] = {}
    restore: list[str] = []

    for group in groups.values():
        winner = min(group, key=_precedence)
        for candidate in group:
            if candidate.id == winner.id or not same_role(candidate, winner):
                # Not superseded by anything. If it is *currently* flagged, that
                # flag is now false and clearing it is this pass's job — which
                # is what makes tightening the rule self-healing: the 685 rows
                # wrongly collapsed under the old key are released on the next
                # run rather than needing a migration to find them.
                if candidate.superseded:
                    restore.append(candidate.id)
                continue
            if candidate.filtered_out and candidate.filter_reason == _reason(winner.id):
                # Already pointing at this winner; rewriting it would bump
                # updated_at on every posting on every run.
                continue
            supersede[candidate.id] = winner.id

    return DedupPlan(supersede=supersede, restore=tuple(sorted(restore)))


def _reason(winner_id: str) -> str:
    """Return the ``filter_reason`` value for a row superseded by ``winner_id``."""
    return f"{SUPERSEDED_PREFIX}{winner_id}"


async def load_candidates(
    session: AsyncSession,
    *,
    company_ids: Sequence[int],
) -> list[DedupCandidate]:
    """Load every open posting for the given companies.

    Args:
        session: The run's session.
        company_ids: Companies whose sources ran. Every member of a dedup group
            shares a ``company_id``, so a group can only have changed if its
            company was touched — which is what makes this scope sufficient
            rather than merely cheap.

    Returns:
        The candidates, including ones already marked superseded: they are how a
        stale supersession is noticed and cleared.
    """
    if not company_ids:
        return []
    rows = await session.execute(
        select(
            JobPosting.id,
            JobPosting.company_id,
            JobPosting.title,
            JobPosting.location_city,
            Source.adapter,
            JobPosting.source_id,
            JobPosting.content_hash,
            JobPosting.first_seen_at,
            JobPosting.filtered_out,
            JobPosting.filter_reason,
        )
        .join(Source, Source.id == JobPosting.source_id)
        .where(
            JobPosting.company_id.in_(company_ids),
            JobPosting.closed_at.is_(None),
        )
    )
    return [
        DedupCandidate(
            id=row.id,
            company_id=row.company_id,
            title=row.title,
            location_city=row.location_city,
            adapter=row.adapter,
            source_id=row.source_id,
            content_hash=row.content_hash,
            first_seen_at=row.first_seen_at,
            filtered_out=row.filtered_out,
            filter_reason=row.filter_reason,
        )
        for row in rows.all()
    ]


async def apply_collapse(session: AsyncSession, plan: DedupPlan) -> None:
    """Write a collapse plan.

    Args:
        session: The run's session.
        plan: The plan from :func:`plan_collapse`.
    """
    by_winner: dict[str, list[str]] = {}
    for loser_id, winner_id in plan.supersede.items():
        by_winner.setdefault(winner_id, []).append(loser_id)

    for winner_id, loser_ids in by_winner.items():
        await session.execute(
            update(JobPosting)
            .where(JobPosting.id.in_(loser_ids))
            .values(filtered_out=True, filter_reason=_reason(winner_id))
        )

    if plan.restore:
        await session.execute(
            update(JobPosting)
            .where(
                JobPosting.id.in_(plan.restore),
                JobPosting.filter_reason.startswith(SUPERSEDED_PREFIX),
            )
            .values(filtered_out=False, filter_reason=None)
        )


async def collapse_duplicates(session: AsyncSession, *, company_ids: Sequence[int]) -> int:
    """Run stage ③'s cross-source collapse for the companies touched by a run.

    Args:
        session: The run's session.
        company_ids: Companies whose sources ran.

    Returns:
        The number of postings newly superseded, for ``run_log.stats``.
    """
    candidates = await load_candidates(session, company_ids=company_ids)
    plan = plan_collapse(candidates)
    if plan.is_empty:
        return 0
    await apply_collapse(session, plan)
    log.info(
        "dedup_collapsed",
        superseded=len(plan.supersede),
        restored=len(plan.restore),
        candidates=len(candidates),
    )
    return len(plan.supersede)


__all__ = [
    "FIDELITY_RANKS",
    "SUPERSEDED_PREFIX",
    "DedupCandidate",
    "DedupPlan",
    "apply_collapse",
    "collapse_duplicates",
    "fidelity_rank",
    "load_candidates",
    "plan_collapse",
    "same_role",
]
