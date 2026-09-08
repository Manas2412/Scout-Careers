"""The composite score. MATCH_SCORING.md §5.

::

    base      = w_hard · H + (1 - w_hard) · N
    composite = min(100, 100 · base · T · R · G)

``coverage_pct`` is ``100 · base`` — the variant-versus-role fit alone, with no
company or timing modifier. ``composite_score`` is the ranking number. Keeping
them separate is the point: coverage answers "can I do this job", composite
answers "should this be the next thing I spend an evening on".

All arithmetic is ``Decimal``. Intermediates carry six places; only the two
stored columns are quantised to ``NUMERIC(5,2)``. Floats are never used —
DATA_MODEL.md §1 — because a score that differs in the sixth place between two
runs makes the A/B comparison in §11 meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from scout_careers.common.config import Settings
from scout_careers.common.types import CompanyTier

#: The two stored columns.
MONEY: Final[Decimal] = Decimal("0.01")

#: Intermediates.
PRECISION: Final[Decimal] = Decimal("0.000001")

#: `R` is quantised to four places before it multiplies, so the worked
#: arithmetic in §5.2 reproduces exactly.
DECAY: Final[Decimal] = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class Composite:
    """The two stored numbers, and the three modifiers that produced one of them.

    The modifiers are returned rather than discarded so a score can be explained
    without recomputing it: "35.15 = 47.50 coverage, x1.00 tier, x0.87 recency,
    x0.85 hard gate" is a sentence the operator can check.
    """

    coverage_pct: Decimal
    composite_score: Decimal
    tier_weight: Decimal
    recency: Decimal
    hard_gate: Decimal


def tier_weight(tier: CompanyTier, settings: Settings) -> Decimal:
    """``T``. A ±10% band, and no more.

    Tier breaks a tie between comparable roles. It must never let a dream-tier
    role the operator cannot do outrank a strong-tier role they can — which is
    exactly what a wider band would buy.
    """
    return {
        CompanyTier.DREAM: settings.scoring_tier_weight_dream,
        CompanyTier.STRONG: settings.scoring_tier_weight_strong,
        CompanyTier.VOLUME: settings.scoring_tier_weight_volume,
    }[tier]


def recency(posted_at: datetime, now: datetime, settings: Settings) -> Decimal:
    """``R``. Grace, then exponential decay to a floor.

    Args:
        posted_at: ``coalesce(posted_at, first_seen_at)``.
        now: Scoring time.
        settings: For the grace, half-life and floor.

    Returns:
        ``1.0000`` inside the grace window, decaying to the floor after it.

    Ages before the grace window — a posting dated in the future, which ATS
    boards do emit — return 1 rather than a value above it. A future date is bad
    data, not a reason to rank something first.
    """
    age = Decimal((now - posted_at).days)
    grace = Decimal(settings.scoring_recency_grace_days)
    if age <= grace:
        return Decimal("1.0000")
    half_life = Decimal(settings.scoring_recency_half_life_days)
    decayed = Decimal(2) ** (-(age - grace) / half_life)
    return max(decayed, settings.scoring_recency_floor).quantize(DECAY)


def hard_gate(hard_coverage: Decimal, settings: Settings) -> Decimal:
    """``G``. The floor gate on hard coverage.

    | ``H``            | ``G``  | Reading                              |
    |------------------|--------|--------------------------------------|
    | ``H ≥ 0.60``     | 1.00   | The must-haves are genuinely covered |
    | ``0.40 ≤ H``     | 0.85   | Real gaps; apply with eyes open      |
    | ``H < 0.40``     | 0.65   | The must-haves are mostly absent     |

    This is what makes hard coverage *dominant* rather than merely
    heavily-weighted. Without it a variant with excellent nice-to-have coverage
    climbs the ranking on a role whose actual requirements it does not meet —
    the failure mode that produces confident, wasted applications.
    """
    if hard_coverage >= settings.scoring_hard_gate_pass:
        return Decimal("1.00")
    if hard_coverage >= settings.scoring_hard_gate_warn:
        return settings.scoring_hard_gate_warn_factor
    return settings.scoring_hard_gate_fail_factor


def composite(
    hard_coverage: Decimal,
    nice_coverage: Decimal,
    *,
    tier: CompanyTier,
    posted_at: datetime,
    now: datetime,
    settings: Settings,
) -> Composite:
    """Blend the two buckets and apply the three modifiers. §5.1.

    Args:
        hard_coverage: ``H``, ``0..1``.
        nice_coverage: ``N``, ``0..1``.
        tier: The company's tier.
        posted_at: ``coalesce(posted_at, first_seen_at)``.
        now: Scoring time.
        settings: The formula's constants.

    Returns:
        Both stored numbers and the modifiers behind them.

    The clamp to 100 exists because ``dream`` tier can push a perfect base above
    it, and ``composite_score`` is ``NUMERIC(5,2)``.
    """
    blend = settings.scoring_blend_hard
    base = (blend * hard_coverage + (Decimal("1") - blend) * nice_coverage).quantize(PRECISION)

    weight = tier_weight(tier, settings)
    decay = recency(posted_at, now, settings)
    gate = hard_gate(hard_coverage, settings)

    score = Decimal("100") * base * weight * decay * gate
    return Composite(
        coverage_pct=(Decimal("100") * base).quantize(MONEY),
        composite_score=min(score, Decimal("100")).quantize(MONEY),
        tier_weight=weight,
        recency=decay,
        hard_gate=gate,
    )


__all__ = [
    "DECAY",
    "MONEY",
    "PRECISION",
    "Composite",
    "composite",
    "hard_gate",
    "recency",
    "tier_weight",
]
