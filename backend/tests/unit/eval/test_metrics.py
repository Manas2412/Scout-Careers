"""The Tier 1 metrics, against worked examples.

These are the numbers a gate decides on, so they are asserted against values
computed independently of the implementation — not against whatever it happens
to return. A scoring gate that is quietly wrong is worse than no gate: it keeps
passing while the thing it guards rots.
"""

from __future__ import annotations

import pytest

from scout_careers.eval.metrics import (
    MAX_REGRESSION,
    TIER1_GATES,
    Gate,
    accuracy,
    cohen_kappa,
    evaluate,
    precision,
    recall,
    regressions,
    spearman_rho,
)

# --------------------------------------------------------------------------
# Ratios, and the empty case
# --------------------------------------------------------------------------


def test_the_plain_ratios() -> None:
    assert recall(9, 10) == pytest.approx(0.9)
    assert precision(17, 20) == pytest.approx(0.85)
    assert accuracy(22, 25) == pytest.approx(0.88)


def test_nothing_to_measure_is_vacuously_satisfied() -> None:
    """A golden case with no `nice` requirements must not drag the `nice`
    metric to zero for every other case in the set. Vacuous, not failed."""
    assert recall(0, 0) == 1.0
    assert precision(0, 0) == 1.0


# --------------------------------------------------------------------------
# Cohen's κ
# --------------------------------------------------------------------------


def test_kappa_on_a_worked_two_by_two() -> None:
    """40 items, 30 agreements, marginals 50/50 both ways.

    Observed 0.75, chance 0.5, so κ = (0.75 - 0.5) / 0.5 = 0.5. Raw agreement
    would have read 0.75 and looked respectable; κ says half of it was luck.
    """
    a = ["m"] * 20 + ["x"] * 20
    b = ["m"] * 15 + ["x"] * 5 + ["m"] * 5 + ["x"] * 15
    assert cohen_kappa(a, b) == pytest.approx(0.5)


def test_kappa_on_three_coverage_levels() -> None:
    a = ["met", "partial", "missing", "met", "missing"]
    b = ["met", "missing", "missing", "met", "missing"]
    assert cohen_kappa(a, b) == pytest.approx(0.666667, abs=1e-6)


def test_perfect_agreement_is_one() -> None:
    labels = ["met", "partial", "missing", "met"]
    assert cohen_kappa(labels, labels) == pytest.approx(1.0)


def test_kappa_punishes_the_all_missing_scorer() -> None:
    """The reason κ is the gate rather than accuracy.

    Most requirements on a real posting come out `missing`, so a scorer that
    answers "missing" to everything looks accurate. Raw agreement here is 0.8;
    κ is 0, which is the truth — it is chance, not skill.
    """
    operator = ["missing"] * 8 + ["met", "partial"]
    lazy = ["missing"] * 10
    raw = sum(1 for x, y in zip(operator, lazy, strict=True) if x == y) / len(operator)
    assert raw == pytest.approx(0.8)
    assert cohen_kappa(operator, lazy) == pytest.approx(0.0)


def test_both_raters_constant_and_agreeing_is_one_not_a_crash() -> None:
    """A case where every requirement really is `missing` is legitimate.
    Chance agreement is 1.0 there, so κ is 0/0; it must not raise."""
    assert cohen_kappa(["missing"] * 5, ["missing"] * 5) == pytest.approx(1.0)


def test_both_raters_constant_and_disagreeing_is_zero() -> None:
    assert cohen_kappa(["met"] * 5, ["missing"] * 5) == pytest.approx(0.0)


def test_misaligned_labellings_are_refused() -> None:
    """Truncating to the shorter would produce a plausible number from two
    things that are not comparable."""
    with pytest.raises(ValueError, match="aligned"):
        cohen_kappa(["met", "missing"], ["met"])


# --------------------------------------------------------------------------
# Spearman ρ
# --------------------------------------------------------------------------


def test_identical_orderings() -> None:
    assert spearman_rho([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)


def test_reversed_orderings() -> None:
    assert spearman_rho([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_ties_take_the_average_rank() -> None:
    """The common case here, and the one the `1 - 6Σd²` shortcut gets wrong:
    six variants, and a terse JD often scores several of them identically."""
    assert spearman_rho([3, 1, 2, 2], [30, 10, 25, 20]) == pytest.approx(0.948683, abs=1e-6)


def test_a_tied_ordering_on_both_sides_is_agreement() -> None:
    """Both say nothing separates these variants. That is agreement, not an
    undefined correlation — and it happens on genuinely generic postings."""
    assert spearman_rho([5, 5, 5], [2, 2, 2]) == pytest.approx(1.0)


def test_one_side_tied_and_the_other_not_is_zero() -> None:
    """One saw a difference, the other did not. Zero is the honest reading."""
    assert spearman_rho([5, 5, 5], [1, 2, 3]) == pytest.approx(0.0)


def test_a_single_variant_cannot_be_ranked() -> None:
    assert spearman_rho([1.0], [9.0]) == pytest.approx(1.0)


def test_unequal_lengths_are_refused() -> None:
    with pytest.raises(ValueError, match="equal lengths"):
        spearman_rho([1, 2, 3], [1, 2])


# --------------------------------------------------------------------------
# The gates
# --------------------------------------------------------------------------


def test_the_documented_gates_are_the_ones_enforced() -> None:
    """MATCH_SCORING.md §12.1. If the doc and the code drift, this says so."""
    assert TIER1_GATES["hard_recall"] == (0.90, True)
    assert TIER1_GATES["hard_precision"] == (0.85, True)
    assert TIER1_GATES["kind_accuracy"] == (0.88, True)
    assert TIER1_GATES["normalisation_accuracy"] == (0.95, True)
    assert TIER1_GATES["coverage_kappa"] == (0.70, True)
    assert TIER1_GATES["ranking_rho"] == (0.75, True)
    assert TIER1_GATES["grounding_violations"] == (0.0, False)


def test_a_metric_exactly_on_its_threshold_passes() -> None:
    assert Gate("hard_recall", 0.90, 0.90).passed


def test_grounding_gates_at_zero_not_at_a_floor() -> None:
    """A fabricated evidence span is invariant-adjacent (ARCHITECTURE.md §3),
    and an invariant with a 95% pass rate is not an invariant."""
    assert Gate("grounding_violations", 0, 0.0, higher_is_better=False).passed
    assert not Gate("grounding_violations", 1, 0.0, higher_is_better=False).passed


def test_a_missing_metric_reads_as_zero_and_fails() -> None:
    """A runner that silently skipped a metric must not produce a green gate."""
    gates = {g.name: g for g in evaluate({"hard_recall": 0.95})}
    assert gates["hard_recall"].passed
    assert not gates["coverage_kappa"].passed


def test_a_regression_inside_every_gate_is_still_reported() -> None:
    """The case AI_ARCHITECTURE.md §10.3 exists for: trading four points of
    recall for one of precision clears both floors and is a bad change."""
    gates = evaluate({"hard_recall": 0.91, "hard_precision": 0.86})
    found = regressions(gates, {"hard_recall": 0.95, "hard_precision": 0.85})
    assert any("hard_recall" in line for line in found)
    assert not any("hard_precision" in line for line in found)


def test_a_small_wobble_is_not_a_regression() -> None:
    gates = evaluate({"hard_recall": 0.94})
    assert not regressions(gates, {"hard_recall": 0.95}, limit=MAX_REGRESSION)


def test_grounding_moving_the_wrong_way_counts_as_a_regression() -> None:
    """Lower is better there, so the comparison has to flip sign."""
    gates = evaluate({"grounding_violations": 3})
    assert regressions(gates, {"grounding_violations": 0})
