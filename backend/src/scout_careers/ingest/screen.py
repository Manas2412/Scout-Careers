"""Stage ④ against the database: run the filter chain and write the verdict.

``ingest/filters.py`` is pure — it decides, and it touches nothing. This module
is the half that persists, and until it existed the filter was measured by a
dry-run script and applied by nobody: ``filtered_out`` was false on 7,161 of
7,200 postings, and the extraction estimate priced them all at ₹9,708 against
₹2,807 for the survivors. A rule nothing enforces is a document, not a filter.

**Screening is a recompute, not an accumulation.** Every posting is re-judged
from the current settings, and a posting that now passes has its flag *cleared*.
That is what makes the deny lists safe to edit: widening ``FILTER_LOCATION_ALLOW``
releases the postings it was hiding, rather than requiring someone to remember
which rows a previous setting wrote. The same property makes this command
idempotent — running it twice changes nothing the second time.

**Superseded rows are not ours to touch.** ``ingest/dedup.py`` writes
``filtered_out`` with a reason of ``superseded_by:<winner id>``, and that ID is
load-bearing: ``plan_collapse`` finds those rows by prefix to restore them when
the winner disappears. Rewriting the reason to a bare ``superseded`` would erase
the pointer and break dedup's self-healing — silently, and only visible as
postings that stay hidden after their duplicate is gone. So a superseded verdict
is skipped here entirely: it is already correct, and it belongs to another
stage.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scout_careers.common.config import Settings
from scout_careers.common.logging import get_logger
from scout_careers.db.models import Company, JobPosting
from scout_careers.ingest.filters import CompanyView, PostingView, evaluate

log = get_logger(__name__)

#: The one verdict this module refuses to write. See the module docstring.
SUPERSEDED_REASON = "superseded"

#: Rows per UPDATE round trip. The corpus is ~7,000 and a single statement with
#: 7,000 bound parameters is a different failure mode from a slow one.
BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class ScreenChange:
    """One posting whose verdict differs from what is stored.

    Attributes:
        posting_id: The posting.
        filtered_out: The new flag.
        filter_reason: The new reason, or ``None`` when it now passes.
    """

    posting_id: str
    filtered_out: bool
    filter_reason: str | None


@dataclass(slots=True)
class ScreenOutcome:
    """What a screening pass did.

    Attributes:
        considered: Postings judged.
        passing: Postings that survive the chain.
        rejected: Postings the chain rejects, including already-rejected ones.
        newly_rejected: Postings this pass hid that were previously visible.
        readmitted: Postings this pass released. Worth its own counter because
            it is the direction that costs money — every readmitted posting is
            an extraction call the next backfill will pay for.
        skipped_superseded: Rows left to ``dedup.py``.
        by_reason: Rejection counts per predicate, for the operator's table.
    """

    considered: int = 0
    passing: int = 0
    rejected: int = 0
    newly_rejected: int = 0
    readmitted: int = 0
    skipped_superseded: int = 0
    by_reason: Counter[str] = field(default_factory=Counter)

    @property
    def changed(self) -> int:
        """Rows this pass would write."""
        return self.newly_rejected + self.readmitted

    def as_stats(self) -> dict[str, object]:
        """The rollup for ``run_log.stats`` and the CLI table."""
        return {
            "screen_considered": self.considered,
            "screen_passing": self.passing,
            "screen_rejected": self.rejected,
            "screen_newly_rejected": self.newly_rejected,
            "screen_readmitted": self.readmitted,
            "screen_skipped_superseded": self.skipped_superseded,
        }


def _views(posting: JobPosting, company: Company) -> tuple[PostingView, CompanyView]:
    """Project the two ORM rows onto what the predicates are allowed to see."""
    return (
        PostingView(
            id=posting.id,
            title=posting.title,
            description_text=posting.description_text or "",
            location_city=posting.location_city,
            location_country=posting.location_country,
            is_remote=posting.is_remote,
            seniority_guess=posting.seniority_guess,
            closed_at=posting.closed_at,
            filtered_out=posting.filtered_out,
            filter_reason=posting.filter_reason,
            raw=posting.raw or {},
        ),
        CompanyView(
            id=company.id,
            slug=company.slug,
            status=company.status,
            location_filter=company.location_filter or (),
        ),
    )


@dataclass(frozen=True, slots=True)
class Judgement:
    """One posting's verdict, and what it implies for the stored flags.

    Attributes:
        reason: The rejection reason, or ``None`` when the posting passes.
        change: What to write, or ``None`` when the stored value already agrees.
        skipped: The row belongs to ``dedup.py`` and was not judged for writing.
    """

    reason: str | None
    change: ScreenChange | None = None
    skipped: bool = False

    @property
    def passed(self) -> bool:
        """Whether the posting survives the chain."""
        return self.reason is None


def judge(posting: PostingView, company: CompanyView, settings: Settings) -> Judgement:
    """Decide one posting's verdict and whether the stored flags need writing.

    Args:
        posting: The posting, carrying its *current* ``filtered_out`` and
            ``filter_reason``.
        company: Its employer.
        settings: Configuration.

    Returns:
        The judgement.

    Pure, and separated from the query for the same reason ``dedup.plan_collapse``
    is: every rule worth arguing about — that a superseded row is untouchable,
    that a passing posting has its flag cleared, that an unchanged verdict is
    not rewritten — is decided here, and can therefore be tested without a
    database.
    """
    verdict = evaluate(posting, company, settings)

    if not verdict.passed and verdict.reason == SUPERSEDED_REASON:
        # dedup.py owns this row, and its reason carries the winner's ID.
        return Judgement(SUPERSEDED_REASON, skipped=True)

    if verdict.passed:
        if posting.filtered_out:
            return Judgement(None, ScreenChange(posting.id, False, None))
        return Judgement(None)

    reason = verdict.reason or "unknown"
    if posting.filtered_out and posting.filter_reason == reason:
        return Judgement(reason)
    return Judgement(reason, ScreenChange(posting.id, True, reason))


async def plan_screen(
    session: AsyncSession,
    settings: Settings,
    *,
    company_ids: Sequence[int] | None = None,
) -> tuple[ScreenOutcome, list[ScreenChange]]:
    """Judge every posting and return what would change. Writes nothing.

    Args:
        session: An open session.
        settings: Supplies the three deny lists and the experience ceiling.
        company_ids: Restrict to these employers. The in-run path passes the
            companies the run touched; a backfill passes nothing.

    Returns:
        The counts, and the rows whose stored verdict is out of date.

    Split from the write so that the dry run and the real thing evaluate
    identical code. A preview that reaches its answer by a different path is a
    preview of something else.
    """
    outcome = ScreenOutcome()
    changes: list[ScreenChange] = []

    stmt = select(JobPosting, Company).join(Company, Company.id == JobPosting.company_id)
    if company_ids is not None:
        stmt = stmt.where(JobPosting.company_id.in_(list(company_ids)))

    rows = await session.stream(stmt)
    async for posting, company in rows:
        outcome.considered += 1
        judgement = judge(*_views(posting, company), settings)

        if judgement.passed:
            outcome.passing += 1
            if judgement.change is not None:
                outcome.readmitted += 1
        else:
            outcome.rejected += 1
            outcome.by_reason[judgement.reason or "unknown"] += 1
            if judgement.skipped:
                outcome.skipped_superseded += 1
            elif not posting.filtered_out:
                outcome.newly_rejected += 1

        if judgement.change is not None:
            changes.append(judgement.change)

    return outcome, changes


async def apply_screen(session: AsyncSession, changes: Sequence[ScreenChange]) -> int:
    """Write the verdicts.

    Args:
        session: An open session. The caller commits.
        changes: From :func:`plan_screen`.

    Returns:
        Rows written.

    Grouped by target verdict rather than issued per row: a recompute over the
    whole corpus is thousands of rows that take one of a handful of distinct
    values, so one statement per value and a bounded ``IN`` list is the shape
    that fits, not seven thousand UPDATEs.
    """
    if not changes:
        return 0

    grouped: dict[tuple[bool, str | None], list[str]] = {}
    for change in changes:
        grouped.setdefault((change.filtered_out, change.filter_reason), []).append(
            change.posting_id
        )

    written = 0
    for (filtered_out, reason), ids in grouped.items():
        for start in range(0, len(ids), BATCH_SIZE):
            chunk = ids[start : start + BATCH_SIZE]
            await session.execute(
                update(JobPosting)
                .where(JobPosting.id.in_(chunk))
                .values(filtered_out=filtered_out, filter_reason=reason)
            )
            written += len(chunk)
    return written


async def screen_postings(
    session: AsyncSession,
    settings: Settings,
    *,
    company_ids: Sequence[int] | None = None,
) -> ScreenOutcome:
    """Judge and write in one pass.

    Args:
        session: An open session. The caller commits.
        settings: Configuration.
        company_ids: Restrict to these employers.

    Returns:
        What happened.
    """
    outcome, changes = await plan_screen(session, settings, company_ids=company_ids)
    written = await apply_screen(session, changes)
    log.info(
        "screen_applied",
        considered=outcome.considered,
        passing=outcome.passing,
        rejected=outcome.rejected,
        newly_rejected=outcome.newly_rejected,
        readmitted=outcome.readmitted,
        written=written,
        top_reasons=dict(outcome.by_reason.most_common(5)),
    )
    return outcome


def reason_families(by_reason: Mapping[str, int]) -> Counter[str]:
    """Collapse ``seniority:director`` and friends onto their predicate name.

    Args:
        by_reason: Exact reasons and their counts.

    Returns:
        Counts per predicate.

    The exact reason is what makes one rejection explainable; the family is what
    makes the filter's *shape* visible. A chain removing 78% where 74 points
    come from one predicate is really one rule with seven decorations, and only
    the grouped view says so.
    """
    families: Counter[str] = Counter()
    for reason, count in by_reason.items():
        families[reason.split(":", 1)[0]] += count
    return families


__all__ = [
    "BATCH_SIZE",
    "SUPERSEDED_REASON",
    "ScreenChange",
    "ScreenOutcome",
    "apply_screen",
    "plan_screen",
    "reason_families",
    "screen_postings",
]
