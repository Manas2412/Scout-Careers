"""The Tier 1 metrics, as arithmetic. MATCH_SCORING.md §12.1.

Pure functions over plain data: no database, no model, no I/O. That is what
lets the gate numbers be unit-tested against worked examples instead of being
believed because the runner printed them.

**Cohen's κ and Spearman ρ are implemented here rather than imported.** scipy is
not a dependency and would be a large one to add for two formulas of a dozen
lines each. Both are written against their definitions with the tie handling
spelled out, because the tie cases are where hand-rolled versions usually go
quietly wrong — and a scoring gate that is quietly wrong is worse than no gate.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

#: Six places, matching MATCH_SCORING.md §5.1 so a metric and a score quantise
#: the same way.
PRECISION = Decimal("0.000001")


def _ratio(numerator: int, denominator: int) -> float:
    """``numerator / denominator``, or 1.0 when there is nothing to measure.

    An empty denominator means the labels asked for nothing of this kind. That
    is vacuously satisfied, not a failure — a golden case with no `nice`
    requirements must not drag `nice` precision to zero for every other case.
    The caller is responsible for not reading a vacuous 1.0 as evidence.
    """
    if denominator == 0:
        return 1.0
    return numerator / denominator


def recall(found: int, labelled: int) -> float:
    """Labelled items the extractor found."""
    return _ratio(found, labelled)


def precision(correct: int, extracted: int) -> float:
    """Extracted items that were labelled."""
    return _ratio(correct, extracted)


def accuracy(correct: int, total: int) -> float:
    """Exact matches over the items where a label exists."""
    return _ratio(correct, total)


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float:
    """Agreement between two labellings, corrected for chance.

    Args:
        a: One rater's labels, e.g. the operator's met/partial/missing.
        b: The other's, same length and same order.

    Returns:
        κ in ``[-1, 1]``. 1 is perfect agreement; 0 is what chance alone would
        produce; negative is worse than chance.

    Raises:
        ValueError: The two sequences are different lengths. Comparing labels
            that are not aligned produces a plausible number from nonsense, so
            this refuses rather than truncating to the shorter.

    Why κ and not plain accuracy: coverage levels are not evenly distributed.
    On a typical posting most requirements come out `missing`, so a scorer that
    answered "missing" to everything would score perhaps 0.7 on raw agreement
    while being useless. κ subtracts exactly that baseline.

    The degenerate case is real and handled: when both raters use a single
    label for everything, chance agreement is 1.0 and κ is undefined (0/0).
    This returns 1.0 when they agree on that label and 0.0 when they do not,
    rather than raising — a golden case where every requirement is genuinely
    `missing` is a legitimate case, not a broken one.
    """
    if len(a) != len(b):
        raise ValueError(f"cohen_kappa needs aligned labellings: {len(a)} vs {len(b)}")
    n = len(a)
    if n == 0:
        return 1.0

    observed = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n

    count_a, count_b = Counter(a), Counter(b)
    expected = sum((count_a[label] / n) * (count_b[label] / n) for label in set(a) | set(b))

    if math.isclose(expected, 1.0):
        return 1.0 if math.isclose(observed, 1.0) else 0.0
    return (observed - expected) / (1 - expected)


def _ranks(values: Sequence[float]) -> list[float]:
    """Competition-free average ranks, 1-based. Ties share the mean of their span.

    Average ranks (not ordinal) are what Spearman is defined on. With ordinal
    ranks, two variants scoring identically would get an arbitrary order and ρ
    would depend on list order — which happens constantly here, because several
    variants often tie on a terse JD.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1  # 1-based mean of the tied span
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman_rho(a: Sequence[float], b: Sequence[float]) -> float:
    """Rank correlation between two orderings.

    Args:
        a: One ordering's values, e.g. the scorer's composite per variant.
        b: The other's, same length and same order.

    Returns:
        ρ in ``[-1, 1]``.

    Raises:
        ValueError: Different lengths.

    Computed as Pearson on average ranks rather than the ``1 - 6Σd²/n(n²-1)``
    shortcut, because that shortcut is only correct when there are no ties, and
    ties are the common case here: six variants, and a terse JD often scores
    several of them identically.

    A constant ordering — every variant scoring the same — has zero variance and
    no defined correlation. That returns 1.0 when both sides are constant (they
    agree that nothing separates the variants) and 0.0 when only one is, which
    is the honest reading of "one side saw a difference and the other did not".
    """
    if len(a) != len(b):
        raise ValueError(f"spearman_rho needs equal lengths: {len(a)} vs {len(b)}")
    n = len(a)
    if n < 2:
        return 1.0

    ra, rb = _ranks(a), _ranks(b)
    mean_a, mean_b = sum(ra) / n, sum(rb) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb, strict=True))
    var_a = sum((x - mean_a) ** 2 for x in ra)
    var_b = sum((y - mean_b) ** 2 for y in rb)

    if math.isclose(var_a, 0.0) and math.isclose(var_b, 0.0):
        return 1.0
    if math.isclose(var_a, 0.0) or math.isclose(var_b, 0.0):
        return 0.0
    return cov / math.sqrt(var_a * var_b)


@dataclass(frozen=True, slots=True)
class Gate:
    """One metric, its measured value, and whether it cleared its threshold."""

    name: str
    value: float
    threshold: float
    #: Grounding violations are a count that must be zero; the rest are ratios
    #: that must be at least the threshold.
    higher_is_better: bool = True

    @property
    def passed(self) -> bool:
        if self.higher_is_better:
            return self.value >= self.threshold
        return self.value <= self.threshold

    def delta(self, baseline: float) -> float:
        return self.value - baseline


#: MATCH_SCORING.md §12.1. Grounding is the only one gated at an exact figure
#: rather than a floor: a fabricated evidence span is the same class of failure
#: as an uncited claim (ARCHITECTURE.md §3, invariant 3), and an invariant with
#: a 95% pass rate is not an invariant.
TIER1_GATES: Mapping[str, tuple[float, bool]] = {
    "hard_recall": (0.90, True),
    "hard_precision": (0.85, True),
    "kind_accuracy": (0.88, True),
    "normalisation_accuracy": (0.95, True),
    "coverage_kappa": (0.70, True),
    "ranking_rho": (0.75, True),
    "grounding_violations": (0.0, False),
}

#: A change may pass every absolute gate and still be a bad change: trading four
#: points of recall for one of precision clears both floors. AI_ARCHITECTURE.md
#: §10.3 makes that a failure too, measured against a named baseline.
MAX_REGRESSION = 0.02


def evaluate(values: Mapping[str, float]) -> tuple[Gate, ...]:
    """Pair each measured value with its gate, in the documented order."""
    return tuple(
        Gate(name, float(values.get(name, 0.0)), threshold, higher)
        for name, (threshold, higher) in TIER1_GATES.items()
    )


def regressions(
    gates: Sequence[Gate], baseline: Mapping[str, float], limit: float = MAX_REGRESSION
) -> tuple[str, ...]:
    """Metrics that fell more than ``limit`` against the baseline, gate or no gate."""
    found: list[str] = []
    for gate in gates:
        if gate.name not in baseline:
            continue
        drop = baseline[gate.name] - gate.value
        if not gate.higher_is_better:
            drop = -drop
        if drop > limit:
            found.append(f"{gate.name}: {baseline[gate.name]:.3f} -> {gate.value:.3f}")
    return tuple(found)


__all__ = [
    "MAX_REGRESSION",
    "PRECISION",
    "TIER1_GATES",
    "Gate",
    "accuracy",
    "cohen_kappa",
    "evaluate",
    "precision",
    "recall",
    "regressions",
    "spearman_rho",
]
