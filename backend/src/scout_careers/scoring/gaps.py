"""The gap list. MATCH_SCORING.md §8.

The composite score orders a queue; the gap list decides what happens to each
item. It is the input to the cover letter's honest-gap paragraph, it is what
tells the operator when *not* to apply, and grouped across postings it answers a
question no job board will: which single missing skill blocks the most roles
this operator would otherwise match.

The sort is the useful part. The first entry is always the biggest reason not to
apply — hard before nice, missing before partial, then document order.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from scout_careers.common.types import SCORED_REQUIREMENT_KINDS, CoverageLevel, RequirementKind
from scout_careers.scoring.coverage import Outcome


def _rank(gap: Outcome) -> tuple[int, int, int]:
    return (
        0 if gap.requirement.kind == RequirementKind.HARD.value else 1,
        0 if gap.level is CoverageLevel.MISSING else 1,
        gap.requirement.ordinal,
    )


def build_gaps(outcomes: Iterable[Outcome]) -> list[dict[str, Any]]:
    """Every scored requirement that did not reach ``met``, worst first.

    Args:
        outcomes: The scored requirements for one (posting, variant) pair.

    Returns:
        The JSONB shape ``match_score.gaps`` stores — the five keys named in
        DATA_MODEL.md §6.1.

    Only scored kinds appear. A ``condition`` — "four ten-hour shifts covering
    weekends" — is something the operator must read before applying, but it is
    not a gap in *them*, and listing it here would put a scheduling sentence at
    the top of a list whose first entry is supposed to be the biggest reason not
    to apply.
    """
    gaps = [
        outcome
        for outcome in outcomes
        if outcome.level is not CoverageLevel.MET
        and outcome.requirement.kind in {kind.value for kind in SCORED_REQUIREMENT_KINDS}
    ]
    return [
        {
            "requirement_id": gap.requirement.id,
            "kind": gap.requirement.kind,
            "level": gap.level.value,
            "text": gap.requirement.text,
            "note": gap.note,
        }
        for gap in sorted(gaps, key=_rank)
    ]


def build_evidence(outcomes: Iterable[Outcome]) -> list[dict[str, Any]]:
    """Every ``met``, with the bullet and claims that earned it. §4.3.

    Ordered by document position so the list reads in the order the requirements
    appear in the posting, which is the order a reviewer checks them against it.
    """
    met = [
        outcome
        for outcome in outcomes
        if outcome.level is CoverageLevel.MET and outcome.evidence is not None
    ]
    met.sort(key=lambda outcome: outcome.requirement.ordinal)
    return [outcome.evidence.as_dict() for outcome in met if outcome.evidence is not None]


def blocking_gaps(gaps: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The hard gaps at level ``missing`` — the reasons to skip.

    Three of these is a skip, and skipping costs one click and no tokens. This
    is the mechanism that keeps the system at five to ten applications a week
    rather than two hundred: the number that says "apply" is not the score, it
    is the absence of disqualifying gaps.
    """
    return [
        gap
        for gap in gaps
        if gap["kind"] == RequirementKind.HARD.value and gap["level"] == CoverageLevel.MISSING.value
    ]


__all__ = ["blocking_gaps", "build_evidence", "build_gaps"]
