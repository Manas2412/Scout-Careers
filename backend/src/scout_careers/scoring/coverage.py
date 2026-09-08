"""Coverage: how well one variant answers one requirement. MATCH_SCORING.md §4.

Entirely pure and entirely deterministic. No model runs here — coverage is a set
operation over ``requirement.normalised_skill`` and ``resume_variant.skill_set``,
both of which speak the same closed vocabulary, which is the whole reason the
vocabulary is closed. Six variants against thirty postings a day is 180
scorings, and none of them costs a token.

The interlock with the claims ledger lives in :func:`_met`. A bullet that states
a number the ledger cannot back is not allowed to *prove* a requirement: it
degrades to ``partial`` and the reason is recorded in the gap note. The
consequence is the one the system is designed around — letting the ledger rot
lowers the operator's own scores.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Self

from scout_careers.common.types import CoverageLevel
from scout_careers.extract.variants import Bullet, iter_bullets
from scout_careers.extract.vocabulary import Vocabulary, get_vocabulary
from scout_careers.scoring.adjacency import Adjacency, get_adjacency

#: Six places, per §5.1: intermediates carry six, only the two stored columns
#: are quantised to ``NUMERIC(5,2)``.
PRECISION: Final[Decimal] = Decimal("0.000001")

#: Any digit outside a bare version-like suffix. Deliberately blunt: the rule it
#: serves is "a quantified bullet needs ledger backing", and the safe error is
#: to call a bullet quantified when it is merely versioned — that costs half
#: credit on one requirement, where the opposite ships an unbacked number.
_DIGIT = re.compile(r"\d")


def contains_numeric(text: str) -> bool:
    """Whether a bullet asserts a number, and so needs a claim behind it."""
    return bool(_DIGIT.search(text))


@dataclass(frozen=True, slots=True)
class ScoredRequirement:
    """One requirement as the scorer sees it.

    A plain value object rather than the ORM row, so every function below can be
    tested without a database — which is what makes the arithmetic in §9 and §10
    reproducible in CI.
    """

    id: int
    kind: str
    text: str
    normalised_skill: str | None
    weight: Decimal
    ordinal: int


@dataclass(frozen=True, slots=True)
class Evidence:
    """The bullet that earned a ``met``, and the claims behind it.

    ``bullet_ref`` is the bullet's own ID, which is a stable path into
    ``resume_variant.content`` — so a reviewer clicking a met requirement in the
    queue lands on the line that earned it.
    """

    requirement_id: int
    requirement: str
    level: str
    bullet_ref: str
    bullet: str
    claim_keys: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """The JSONB shape ``match_score.evidence`` stores (§4.3)."""
        return {
            "requirement_id": self.requirement_id,
            "requirement": self.requirement,
            "level": self.level,
            "bullet_ref": self.bullet_ref,
            "bullet": self.bullet,
            "claim_keys": list(self.claim_keys),
        }


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one requirement scored, and why."""

    requirement: ScoredRequirement
    level: CoverageLevel
    evidence: Evidence | None
    note: str | None


@dataclass(frozen=True, slots=True)
class VariantView:
    """A variant reduced to what scoring needs.

    Built once per variant per run rather than per requirement: walking the
    bullets of six resumes for every one of thirty requirements on every one of
    thirty postings is the difference between milliseconds and minutes.
    """

    id: int
    key: str
    skill_set: frozenset[str]
    bullets: tuple[Bullet, ...]

    @classmethod
    def build(
        cls, *, variant_id: int, key: str, skill_set: Iterable[str], content: dict[str, Any]
    ) -> Self:
        return cls(
            id=variant_id,
            key=key,
            skill_set=frozenset(skill_set),
            bullets=tuple(iter_bullets(content)),
        )

    def bullets_for(self, token: str) -> tuple[Bullet, ...]:
        """Every bullet tagged with ``token``, in document order."""
        return tuple(bullet for bullet in self.bullets if token in bullet.skills)


def _best_bullet(bullets: Sequence[Bullet]) -> Bullet:
    """The bullet that best proves a token.

    Most ledger-backed claims first, then earliest in the document. Claims win
    over position because a cited number is what survives a screen; position
    breaks the tie because the top of a resume is what gets read.
    """
    ranked = min(enumerate(bullets), key=lambda pair: (-len(pair[1].claim_keys), pair[0]))
    return ranked[1]


def _met(
    requirement: ScoredRequirement, variant: VariantView, token: str
) -> tuple[CoverageLevel, Evidence | None, str | None]:
    """Resolve a token the variant holds into a level and its evidence."""
    bullets = variant.bullets_for(token)
    if not bullets:
        # The token is in `skill_set` but nothing in the body demonstrates it.
        # Honest half-credit: the operator did claim it on their skills line,
        # and generation will have no sentence to cite for it.
        return (
            CoverageLevel.PARTIAL,
            None,
            "Listed as a skill; no bullet demonstrates it.",
        )

    bullet = _best_bullet(bullets)
    if contains_numeric(bullet.text) and not bullet.claim_keys:
        # The ledger interlock. A quantified bullet with nothing behind the
        # number cannot be cited as proof — of anything, to anyone.
        return (
            CoverageLevel.PARTIAL,
            None,
            "Bullet is quantified but carries no claim keys.",
        )

    return (
        CoverageLevel.MET,
        Evidence(
            requirement_id=requirement.id,
            requirement=requirement.text,
            level=CoverageLevel.MET.value,
            bullet_ref=bullet.id,
            bullet=bullet.text,
            claim_keys=bullet.claim_keys,
        ),
        None,
    )


def level_for(
    requirement: ScoredRequirement,
    variant: VariantView,
    *,
    vocabulary: Vocabulary | None = None,
    adjacency: Adjacency | None = None,
    adjacency_min: Decimal = Decimal("0.40"),
) -> Outcome:
    """Score one requirement against one variant. §4.2.

    Args:
        requirement: The extracted requirement.
        variant: The variant's skills and bullets.
        vocabulary: Injectable; defaults to the packaged one.
        adjacency: Injectable; defaults to the packaged table.
        adjacency_min: ``SKILL_ADJACENCY_MIN``.

    Returns:
        The level, the evidence when ``met``, and a note when there is something
        to say about why.
    """
    vocab = vocabulary or get_vocabulary()
    table = adjacency or get_adjacency()
    token = requirement.normalised_skill

    if token is None:
        # Never silently dropped: an unresolvable requirement is a real gap the
        # operator reads, and the phrase is a `skill_proposal` candidate.
        return Outcome(
            requirement,
            CoverageLevel.MISSING,
            None,
            "Requirement did not resolve to a vocabulary token.",
        )

    if members := vocab.members_of(token):
        # A composite is scored on the fraction of its members held, which is
        # more precise than adjacency: it can name which halves are missing.
        hits = sorted(member for member in members if member in variant.skill_set)
        if len(hits) == len(members):
            level, evidence, note = _met(requirement, variant, hits[0])
            return Outcome(requirement, level, evidence, note)
        if hits:
            absent = sorted(set(members) - set(hits))
            level, evidence, _ = _met(requirement, variant, hits[0])
            note = f"Covers {', '.join(hits)}; no evidence for {', '.join(absent)}."
            return Outcome(
                requirement,
                CoverageLevel.PARTIAL if level is CoverageLevel.MET else level,
                evidence if level is CoverageLevel.MET else None,
                note,
            )
        return Outcome(
            requirement,
            CoverageLevel.MISSING,
            None,
            f"No evidence for any of {', '.join(sorted(members))}.",
        )

    if token in variant.skill_set:
        level, evidence, note = _met(requirement, variant, token)
        return Outcome(requirement, level, evidence, note)

    # Adjacency earns `partial`, never `met`. A neighbouring competency is
    # evidence that the operator could learn this quickly, not evidence that
    # they have done it.
    near = table.best(token, variant.skill_set, vocabulary=vocab)
    if near is not None and near.score >= adjacency_min:
        return Outcome(
            requirement,
            CoverageLevel.PARTIAL,
            None,
            f"Adjacent evidence: {vocab.skills[near.token].label}.",
        )

    return Outcome(requirement, CoverageLevel.MISSING, None, None)


def bucket_coverage(
    outcomes: Sequence[Outcome], *, partial_credit: Decimal = Decimal("0.5")
) -> Decimal:
    """Weighted coverage of one bucket, ``0..1``. §4.4.

    Args:
        outcomes: The scored requirements in this bucket.
        partial_credit: ``SCORING_PARTIAL_CREDIT``.

    Returns:
        ``earned / total``, six decimal places.

    An empty bucket returns 1 — it is satisfied, having asked for nothing. For
    the *hard* bucket that is also a red flag rather than a perfect match, and
    the caller routes it to ``needs_manual_review`` instead of scoring it (§13.2).
    """
    credit = {
        CoverageLevel.MET: Decimal("1"),
        CoverageLevel.PARTIAL: partial_credit,
        CoverageLevel.MISSING: Decimal("0"),
    }
    total = sum((outcome.requirement.weight for outcome in outcomes), Decimal("0"))
    if total == 0:
        return Decimal("1")
    earned = sum(
        (outcome.requirement.weight * credit[outcome.level] for outcome in outcomes),
        Decimal("0"),
    )
    return (earned / total).quantize(PRECISION)


def met_count(outcomes: Iterable[Outcome]) -> int:
    """Unweighted count of rows at ``met``, for display.

    Deliberately not what the score is computed from, and the two will not
    agree: a variant can be 4/7 on hard requirements and still score badly if
    the three it missed carry most of the weight. That is the design working.
    """
    return sum(1 for outcome in outcomes if outcome.level is CoverageLevel.MET)


__all__ = [
    "PRECISION",
    "Evidence",
    "Outcome",
    "ScoredRequirement",
    "VariantView",
    "bucket_coverage",
    "contains_numeric",
    "level_for",
    "met_count",
]
