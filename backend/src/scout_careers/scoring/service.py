"""Scoring a posting against every variant, and choosing a winner.

Stage ⑥. The pure half — :func:`score_variant`, :func:`rank` — takes value
objects and returns value objects, so the worked arithmetic in MATCH_SCORING.md
§9 and §10 is reproducible in a unit test with no database. The persistence half
writes ``match_score`` rows and sets exactly one ``is_recommended``.

**Fail open on enrichment, fail closed on integrity.** One variant raising does
not lose the other five: that variant's row is skipped and logged, and the
remaining variants still produce a recommendation (§13.2). A posting with zero
extracted hard requirements is the opposite case — it is not scored as a perfect
match, it is flagged, because an empty hard bucket returning ``H = 1`` is the
single most dangerous default in the formula.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_careers.common.config import Settings
from scout_careers.common.types import (
    HARD_REQUIREMENT_KINDS,
    NICE_REQUIREMENT_KINDS,
    CompanyTier,
    CoverageLevel,
)
from scout_careers.db.models import Company, JobPosting, MatchScore, Requirement, ResumeVariant
from scout_careers.extract.vocabulary import Vocabulary, get_vocabulary
from scout_careers.scoring.adjacency import Adjacency, get_adjacency
from scout_careers.scoring.composite import Composite, composite
from scout_careers.scoring.coverage import (
    Outcome,
    ScoredRequirement,
    VariantView,
    bucket_coverage,
    level_for,
    met_count,
)
from scout_careers.scoring.gaps import build_evidence, build_gaps

logger = logging.getLogger(__name__)


def score_version(settings: Settings, adjacency: Adjacency) -> str:
    """``score.vN+adjacency.YYYY-MM-DD.N`` — the provenance of one score.

    Both halves, for the same reason ``requirement.prompt_version`` carries the
    vocabulary: an adjacency edit changes every score it touches, and a row that
    cited only the formula version could not be told apart from one scored
    before that edit. Because ``prompt_version`` is part of the unique key, a
    change here *inserts* beside the old rows rather than overwriting the
    comparison that shows whether it was an improvement (§11.3).
    """
    return f"{settings.scoring_prompt_version}+{adjacency.version}"


@dataclass(frozen=True, slots=True)
class VariantScore:
    """One (posting, variant) pair, scored."""

    variant: VariantView
    outcomes: tuple[Outcome, ...]
    hard: tuple[Outcome, ...]
    nice: tuple[Outcome, ...]
    hard_coverage: Decimal
    nice_coverage: Decimal
    result: Composite

    @property
    def hard_missing(self) -> int:
        """Hard requirements at ``missing`` — tie-break 3, and the skip signal."""
        return sum(1 for outcome in self.hard if outcome.level is CoverageLevel.MISSING)


def score_variant(
    requirements: Sequence[ScoredRequirement],
    variant: VariantView,
    *,
    tier: CompanyTier,
    posted_at: datetime,
    now: datetime,
    settings: Settings,
    vocabulary: Vocabulary | None = None,
    adjacency: Adjacency | None = None,
) -> VariantScore:
    """Score one variant against one posting's requirements. Pure.

    Args:
        requirements: Every extracted requirement, of any kind. Unscored kinds
            are filtered here rather than by the caller, so a caller cannot
            accidentally widen the denominator.
        variant: The variant's skills and bullets.
        tier: The company's tier.
        posted_at: ``coalesce(posted_at, first_seen_at)``.
        now: Scoring time.
        settings: The formula's constants.
        vocabulary: Injectable.
        adjacency: Injectable.

    Returns:
        The outcomes, both bucket coverages, and the composite.
    """
    vocab = vocabulary or get_vocabulary()
    table = adjacency or get_adjacency()
    hard_kinds = {kind.value for kind in HARD_REQUIREMENT_KINDS}
    nice_kinds = {kind.value for kind in NICE_REQUIREMENT_KINDS}

    outcomes = tuple(
        level_for(
            requirement,
            variant,
            vocabulary=vocab,
            adjacency=table,
            adjacency_min=settings.skill_adjacency_min,
        )
        for requirement in requirements
        if requirement.kind in hard_kinds | nice_kinds
    )
    hard = tuple(outcome for outcome in outcomes if outcome.requirement.kind in hard_kinds)
    nice = tuple(outcome for outcome in outcomes if outcome.requirement.kind in nice_kinds)

    credit = settings.scoring_partial_credit
    hard_coverage = bucket_coverage(hard, partial_credit=credit)
    nice_coverage = bucket_coverage(nice, partial_credit=credit)

    return VariantScore(
        variant=variant,
        outcomes=outcomes,
        hard=hard,
        nice=nice,
        hard_coverage=hard_coverage,
        nice_coverage=nice_coverage,
        result=composite(
            hard_coverage,
            nice_coverage,
            tier=tier,
            posted_at=posted_at,
            now=now,
            settings=settings,
        ),
    )


#: Tie-break 2 fires only when the top two are this close. Below it the
#: composite has already said something meaningful and should be respected.
TIE_BAND = Decimal("1.00")


def rank(scores: Sequence[VariantScore], *, default_variant_id: int | None = None) -> VariantScore:
    """Choose the winning variant. §6.1.

    Args:
        scores: At least one scored variant.
        default_variant_id: ``company.default_variant_id`` — the operator's
            standing preference for this employer.

    Returns:
        The winner.

    Raises:
        ValueError: When there is nothing to rank.

    The composite selects the *contenders* — everything within ``TIE_BAND`` of
    the leader — and then stops being consulted. Inside the band the order is
    hard coverage, fewest hard requirements at ``missing``, the operator's
    default variant, lowest variant ID.

    Composite deliberately does not reappear as the first key inside the band.
    It did in the first version, and rule 2 could then never fire: sorting by
    composite first means a variant 0.4 behind is already ranked below,
    whatever its hard coverage. The doc's rule only means something if being
    inside the band makes the composite *stop mattering* — which is the point,
    since a gap of under a point is exactly the rounding artefact the tie-break
    exists to overrule.

    Rule 5 in the doc — observed response rate, gated on ≥ 20 submissions — is
    not implemented: ``v_funnel`` has no rows yet, and a tie-break reading an
    empty view is a branch that has never once run.

    The last rule is not decoration. Without a total order a rescore can return
    a different winner from identical inputs, and §11's A/B comparison measures
    the shuffle instead of the change.
    """
    if not scores:
        raise ValueError("nothing to rank")

    leader = max(score.result.composite_score for score in scores)
    contenders = [score for score in scores if leader - score.result.composite_score <= TIE_BAND]

    return min(
        contenders,
        key=lambda score: (
            -score.hard_coverage,
            score.hard_missing,
            0 if score.variant.id == default_variant_id else 1,
            score.variant.id,
        ),
    )


@dataclass(frozen=True, slots=True)
class PostingScored:
    """What scoring one posting produced, for the caller to report."""

    posting_id: str
    scores: tuple[VariantScore, ...]
    winner: VariantScore | None
    skipped: tuple[str, ...]
    note: str | None


def plan_posting(
    requirements: Sequence[ScoredRequirement],
    variants: Sequence[VariantView],
    *,
    posting_id: str,
    tier: CompanyTier,
    posted_at: datetime,
    now: datetime,
    settings: Settings,
    default_variant_id: int | None = None,
    vocabulary: Vocabulary | None = None,
    adjacency: Adjacency | None = None,
) -> PostingScored:
    """Score every variant against one posting and pick a winner. Pure.

    A posting with no ``hard`` requirements is returned unscored, with a note.
    An empty hard bucket makes ``H = 1`` by the rule in §4.4 — correct for the
    nice bucket, catastrophic for this one, because it would rank a job
    description the extractor failed on above every real match. §13.2: an
    unscoreable posting is visible and flagged, never silently scored.
    """
    hard_kinds = {kind.value for kind in HARD_REQUIREMENT_KINDS}
    if not any(requirement.kind in hard_kinds for requirement in requirements):
        return PostingScored(
            posting_id=posting_id,
            scores=(),
            winner=None,
            skipped=(),
            note="No hard requirements extracted; needs manual review.",
        )

    scores: list[VariantScore] = []
    skipped: list[str] = []
    for variant in variants:
        try:
            scores.append(
                score_variant(
                    requirements,
                    variant,
                    tier=tier,
                    posted_at=posted_at,
                    now=now,
                    settings=settings,
                    vocabulary=vocabulary,
                    adjacency=adjacency,
                )
            )
        except Exception:
            # Fail open on enrichment: five good rows and one logged failure
            # beat no recommendation at all. Never the posting's text in the
            # log line (invariant §11.2) — the variant key is enough to find it.
            logger.exception("scoring failed for variant %s", variant.key)
            skipped.append(variant.key)

    if not scores:
        return PostingScored(
            posting_id=posting_id,
            scores=(),
            winner=None,
            skipped=tuple(skipped),
            note="Every variant failed to score.",
        )

    return PostingScored(
        posting_id=posting_id,
        scores=tuple(scores),
        winner=rank(scores, default_variant_id=default_variant_id),
        skipped=tuple(skipped),
        note=None,
    )


async def load_variants(session: AsyncSession) -> list[VariantView]:
    """Every variant, reduced to what scoring needs."""
    rows = (await session.execute(select(ResumeVariant).order_by(ResumeVariant.id))).scalars()
    return [
        VariantView.build(
            variant_id=row.id, key=row.key, skill_set=row.skill_set or (), content=row.content or {}
        )
        for row in rows
    ]


async def load_requirements(session: AsyncSession, posting_id: str) -> list[ScoredRequirement]:
    """One posting's requirements, in document order."""
    rows = (
        await session.execute(
            select(Requirement)
            .where(Requirement.posting_id == posting_id)
            .order_by(Requirement.ordinal)
        )
    ).scalars()
    return [
        ScoredRequirement(
            id=row.id,
            kind=row.kind.value,
            text=row.text_,
            normalised_skill=row.normalised_skill,
            weight=row.weight,
            ordinal=row.ordinal,
        )
        for row in rows
    ]


async def store_scores(
    session: AsyncSession,
    scored: PostingScored,
    *,
    prompt_version: str,
    model: str,
    now: datetime,
) -> int:
    """Replace this posting's scores for this version. Returns rows written.

    Delete-then-insert within the caller's transaction, scoped to
    ``prompt_version``: a rescore under the *same* version replaces its own
    rows, and a score under a *new* version lands beside the old family rather
    than destroying it. That is what makes §11.3's A/B query possible at all.

    ``is_recommended`` is set on exactly one row, which the partial unique index
    ``match_one_recommendation_idx`` also enforces — application code and the
    database agree, and the database is the one that cannot be bypassed.
    """
    await session.execute(
        delete(MatchScore).where(
            MatchScore.posting_id == scored.posting_id,
            MatchScore.prompt_version == prompt_version,
        )
    )
    if scored.winner is None:
        return 0

    winner_id = scored.winner.variant.id
    for score in scored.scores:
        session.add(
            MatchScore(
                posting_id=scored.posting_id,
                variant_id=score.variant.id,
                hard_met=met_count(score.hard),
                hard_total=len(score.hard),
                nice_met=met_count(score.nice),
                nice_total=len(score.nice),
                coverage_pct=score.result.coverage_pct,
                composite_score=score.result.composite_score,
                gaps=build_gaps(score.outcomes),
                evidence=build_evidence(score.outcomes),
                is_recommended=score.variant.id == winner_id,
                model=model,
                prompt_version=prompt_version,
                scored_at=now,
            )
        )
    return len(scored.scores)


async def posting_context(
    session: AsyncSession, posting_id: str
) -> tuple[JobPosting, CompanyTier, int | None] | None:
    """The posting, its company's tier, and the operator's default variant."""
    row = (
        await session.execute(
            select(JobPosting, Company)
            .join(Company, Company.id == JobPosting.company_id)
            .where(JobPosting.id == posting_id)
        )
    ).first()
    if row is None:
        return None
    posting, company = row
    return posting, company.tier, company.default_variant_id


__all__ = [
    "TIE_BAND",
    "PostingScored",
    "VariantScore",
    "load_requirements",
    "load_variants",
    "plan_posting",
    "posting_context",
    "rank",
    "score_variant",
    "score_version",
    "store_scores",
]
