"""Coverage, the composite, ranking and gaps. MATCH_SCORING.md §4–§8.

Every function under test is pure, so all of it runs without a database — which
is the point of the import-linter contract that keeps `scoring.coverage`,
`scoring.composite`, `scoring.gaps` and `scoring.adjacency` away from `db`. The
doc's worked arithmetic in §5.2 is reproduced here digit for digit; if the
formula drifts, that test says so in the language the doc uses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scout_careers.common.types import CompanyTier, CoverageLevel, RequirementKind
from scout_careers.extract.variants import Bullet
from scout_careers.scoring.adjacency import Adjacency, get_adjacency, load_adjacency
from scout_careers.scoring.composite import Composite, composite, hard_gate, recency, tier_weight
from scout_careers.scoring.coverage import (
    Outcome,
    ScoredRequirement,
    VariantView,
    bucket_coverage,
    contains_numeric,
    level_for,
    met_count,
)
from scout_careers.scoring.gaps import blocking_gaps, build_evidence, build_gaps
from scout_careers.scoring.service import (
    TIE_BAND,
    VariantScore,
    plan_posting,
    rank,
    score_variant,
    score_version,
)
from tests.conftest import make_settings

NOW = datetime(2026, 9, 5, tzinfo=UTC)


def requirement(
    *,
    row_id: int = 1,
    kind: str = "hard",
    text: str = "Python",
    skill: str | None = "python",
    weight: str = "1.00",
    ordinal: int = 0,
) -> ScoredRequirement:
    return ScoredRequirement(
        id=row_id,
        kind=kind,
        text=text,
        normalised_skill=skill,
        weight=Decimal(weight),
        ordinal=ordinal,
    )


def variant(
    *,
    variant_id: int = 1,
    key: str = "backend",
    skills: tuple[str, ...] = ("python",),
    bullets: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
        ("b.0", "Built the service in Python.", ("python",), ()),
    ),
) -> VariantView:
    """A variant view from raw bullet tuples: (id, text, skills, claim_keys)."""
    content = {
        "experience": [
            {
                "blocks": [
                    {
                        "bullets": [
                            {
                                "id": bullet_id,
                                "text": text,
                                "skills": list(skills_),
                                "claim_keys": list(claims),
                            }
                            for bullet_id, text, skills_, claims in bullets
                        ]
                    }
                ]
            }
        ]
    }
    return VariantView.build(variant_id=variant_id, key=key, skill_set=skills, content=content)


# --------------------------------------------------------------------------
# §4.2 — the three levels
# --------------------------------------------------------------------------


def test_a_token_with_a_clean_bullet_is_met() -> None:
    outcome = level_for(requirement(), variant())
    assert outcome.level is CoverageLevel.MET
    assert outcome.evidence is not None
    assert outcome.evidence.bullet_ref == "b.0"


def test_a_token_the_variant_does_not_hold_is_missing() -> None:
    outcome = level_for(requirement(text="Rust", skill="rust"), variant())
    assert outcome.level is CoverageLevel.MISSING


def test_a_requirement_that_resolved_to_nothing_is_missing_with_a_reason() -> None:
    """Never silently dropped. An unresolvable requirement still shows up as a
    gap the operator reads; discarding it would quietly raise the score."""
    outcome = level_for(requirement(text="Comfortable with ambiguity", skill=None), variant())
    assert outcome.level is CoverageLevel.MISSING
    assert outcome.note is not None
    assert "did not resolve" in outcome.note


def test_a_skill_listed_but_demonstrated_by_no_bullet_is_partial() -> None:
    """Honest half-credit. The operator did claim it on their skills line, and
    generation will have no sentence to cite for it."""
    outcome = level_for(
        requirement(text="Rust", skill="rust"),
        variant(skills=("python", "rust")),
    )
    assert outcome.level is CoverageLevel.PARTIAL
    assert outcome.evidence is None
    assert outcome.note == "Listed as a skill; no bullet demonstrates it."


def test_a_quantified_bullet_with_no_claim_degrades_to_partial() -> None:
    """The ledger interlock, and the whole reason the ledger is load-bearing
    before a single document is generated.

    A bullet that asserts a number the ledger cannot back is not allowed to
    *prove* a requirement. The consequence is the incentive the system is built
    around: letting the ledger rot lowers the operator's own scores.
    """
    outcome = level_for(
        requirement(),
        variant(bullets=(("b.0", "Shipped 312 tests green.", ("python",), ()),)),
    )
    assert outcome.level is CoverageLevel.PARTIAL
    assert outcome.note == "Bullet is quantified but carries no claim keys."


def test_the_same_bullet_with_a_claim_is_met() -> None:
    outcome = level_for(
        requirement(),
        variant(bullets=(("b.0", "Shipped 312 tests green.", ("python",), ("pqbot.tests",)),)),
    )
    assert outcome.level is CoverageLevel.MET
    assert outcome.evidence is not None
    assert outcome.evidence.claim_keys == ("pqbot.tests",)


def test_an_unquantified_bullet_needs_no_claim() -> None:
    """The rule is about numbers, not about every sentence. "Built the service
    in Python" asserts nothing the ledger could verify."""
    assert level_for(requirement(), variant()).level is CoverageLevel.MET


def test_the_bullet_with_the_most_claims_is_chosen() -> None:
    """A cited number is what survives a screen, so it outranks position."""
    outcome = level_for(
        requirement(),
        variant(
            bullets=(
                ("b.0", "Wrote Python.", ("python",), ()),
                ("b.1", "Wrote 60,000 lines of Python.", ("python",), ("pqbot.loc",)),
            )
        ),
    )
    assert outcome.evidence is not None
    assert outcome.evidence.bullet_ref == "b.1"


@pytest.mark.parametrize(
    ("text", "quantified"),
    [
        ("Shipped 312 tests.", True),
        ("Built the service.", False),
        ("Deployed on Postgres 16.", True),
    ],
)
def test_contains_numeric_is_deliberately_blunt(text: str, quantified: bool) -> None:
    """ "Postgres 16" is a version, not a claim, and this calls it quantified.

    The safe error: a bullet wrongly called quantified costs half credit on one
    requirement. The opposite error ships an unbacked number to an employer.
    """
    assert contains_numeric(text) is quantified


# --------------------------------------------------------------------------
# §4.2 — composites
# --------------------------------------------------------------------------


def test_a_composite_with_every_member_held_is_met() -> None:
    held = ("budgeting", "forecasting", "variance_analysis")
    outcome = level_for(
        requirement(text="FP&A fundamentals", skill="fpna"),
        variant(
            skills=held,
            bullets=(("b.0", "Ran the budget.", held, ()),),
        ),
    )
    assert outcome.level is CoverageLevel.MET


def test_a_composite_with_some_members_names_the_missing_ones() -> None:
    """More precise than adjacency: the note says which halves are absent, and
    that sentence is what the cover letter's honest-gap paragraph is built from."""
    outcome = level_for(
        requirement(text="FP&A fundamentals", skill="fpna"),
        variant(
            skills=("variance_analysis",),
            bullets=(("b.0", "Ran variance analysis.", ("variance_analysis",), ()),),
        ),
    )
    assert outcome.level is CoverageLevel.PARTIAL
    assert outcome.note == "Covers variance_analysis; no evidence for budgeting, forecasting."


def test_a_composite_with_no_members_is_missing() -> None:
    outcome = level_for(requirement(text="FP&A", skill="fpna"), variant())
    assert outcome.level is CoverageLevel.MISSING
    assert outcome.note is not None
    assert "No evidence for any of" in outcome.note


# --------------------------------------------------------------------------
# §4.2 — adjacency
# --------------------------------------------------------------------------


def test_a_sibling_in_a_scoring_family_earns_partial() -> None:
    """Azure held, GCP asked. A requirement written "experience with GCP" is
    nearly always asking for cloud-native architecture rather than for GCP's
    console."""
    outcome = level_for(
        requirement(text="GCP", skill="gcp"),
        variant(skills=("azure",), bullets=(("b.0", "Shipped on Azure.", ("azure",), ()),)),
    )
    assert outcome.level is CoverageLevel.PARTIAL
    assert outcome.note is not None
    assert "Adjacent evidence" in outcome.note


def test_adjacency_never_reaches_met() -> None:
    """A neighbouring competency is evidence the operator could learn this
    quickly, not evidence they have done it."""
    outcome = level_for(
        requirement(text="GCP", skill="gcp"),
        variant(skills=("azure",), bullets=(("b.0", "Shipped on Azure.", ("azure",), ()),)),
    )
    assert outcome.level is not CoverageLevel.MET
    assert outcome.evidence is None


def test_a_loose_family_earns_nothing() -> None:
    """`git` must never earn credit for a Kubernetes requirement.

    Both are `platform`, and that family is scored 0.00 on purpose. Overstating
    coverage tells the operator they are a fit when they are not, which is the
    error that costs an evening; understating is the right direction to be
    wrong in.
    """
    outcome = level_for(
        requirement(text="Kubernetes", skill="kubernetes"),
        variant(skills=("git",), bullets=(("b.0", "Used git.", ("git",), ()),)),
    )
    assert outcome.level is CoverageLevel.MISSING


def test_adjacency_below_the_threshold_is_ignored() -> None:
    table = Adjacency(version="test.1", families={"cloud": Decimal("0.30")})
    outcome = level_for(
        requirement(text="GCP", skill="gcp"),
        variant(skills=("azure",), bullets=(("b.0", "Shipped on Azure.", ("azure",), ()),)),
        adjacency=table,
        adjacency_min=Decimal("0.40"),
    )
    assert outcome.level is CoverageLevel.MISSING


def test_the_shipped_adjacency_file_loads() -> None:
    table = load_adjacency()
    assert table.version.startswith("adjacency.")
    assert table.families, "an empty table would silently disable every partial"


def test_the_shipped_adjacency_names_only_real_families() -> None:
    """A family renamed in `skills.yaml` and not here would silently stop
    producing adjacency: every affected requirement would drop from `partial` to
    `missing`, scores would move, and nothing would say why."""
    load_adjacency()  # raises AdjacencyError if a family is unknown


def test_the_loose_families_are_below_the_threshold() -> None:
    """Pinned, because raising one of these is exactly the change that looks
    harmless in a diff and silently inflates every score."""
    table = get_adjacency()
    for family in ("platform", "language", "backend", "ai", "practice"):
        assert table.score_for(family) < Decimal("0.40"), family


# --------------------------------------------------------------------------
# §4.4 — weighted coverage
# --------------------------------------------------------------------------


def outcome_at(
    level: CoverageLevel, weight: str, *, kind: str = "hard", ordinal: int = 0
) -> Outcome:
    return Outcome(
        requirement=requirement(kind=kind, weight=weight, ordinal=ordinal, row_id=ordinal + 1),
        level=level,
        evidence=None,
        note=None,
    )


def test_weighted_coverage_is_earned_over_total() -> None:
    rows = [
        outcome_at(CoverageLevel.MISSING, "2.25", ordinal=0),
        outcome_at(CoverageLevel.MISSING, "1.75", ordinal=1),
        outcome_at(CoverageLevel.PARTIAL, "1.50", ordinal=2),
        outcome_at(CoverageLevel.MET, "0.60", ordinal=3),
        outcome_at(CoverageLevel.MET, "0.60", ordinal=4),
        outcome_at(CoverageLevel.MET, "0.90", ordinal=5),
    ]
    # The §9.1 weights: earned 0.75 + 0.60 + 0.60 + 0.90 = 2.85 over total 7.60.
    assert bucket_coverage(rows) == Decimal("0.375000")


def test_an_empty_bucket_is_satisfied() -> None:
    """It asked for nothing. For the *hard* bucket this is also a red flag, and
    `plan_posting` refuses to score such a posting rather than calling it
    perfect."""
    assert bucket_coverage([]) == Decimal("1")


def test_the_stored_counts_are_unweighted_and_need_not_agree_with_the_score() -> None:
    """A variant can be 2/3 on hard requirements and still score badly if the
    one it missed carries most of the weight. That is the design working."""
    rows = [
        outcome_at(CoverageLevel.MET, "0.10", ordinal=0),
        outcome_at(CoverageLevel.MET, "0.10", ordinal=1),
        outcome_at(CoverageLevel.MISSING, "2.25", ordinal=2),
    ]
    assert met_count(rows) == 2
    assert bucket_coverage(rows) < Decimal("0.10")


# --------------------------------------------------------------------------
# §5 — the composite
# --------------------------------------------------------------------------


def test_the_worked_arithmetic_from_section_5_2() -> None:
    """The doc's miniature, digit for digit.

    A `strong`-tier posting 23 days old, H = 0.4063, N = 0.7500:
    base 0.475000 → coverage 47.50; R = 2^(-9/45) = 0.8706; G = 0.85;
    composite = 35.15.
    """
    config = make_settings()
    result = composite(
        Decimal("0.406250"),
        Decimal("0.750000"),
        tier=CompanyTier.STRONG,
        posted_at=NOW - timedelta(days=23),
        now=NOW,
        settings=config,
    )
    assert result.coverage_pct == Decimal("47.50")
    assert result.recency == Decimal("0.8706")
    assert result.hard_gate == Decimal("0.85")
    assert result.tier_weight == Decimal("1.00")
    assert result.composite_score == Decimal("35.15")


@pytest.mark.parametrize(
    ("days", "expected"),
    [(0, "1.0000"), (14, "1.0000"), (23, "0.8706"), (30, "0.7816"), (400, "0.6500")],
)
def test_recency_decays_to_a_floor(days: int, expected: str) -> None:
    """An old posting is worth less, not worthless — some of the best-matched
    roles sit open for months.

    45 days past the grace window is one half-life, giving 0.5000 — which the
    0.65 floor clamps. Every age beyond that returns the floor, so the decay is
    only observable in the ~31-day window between grace and the clamp.
    """
    assert recency(NOW - timedelta(days=days), NOW, make_settings()) == Decimal(expected)


def test_a_posting_dated_in_the_future_scores_no_bonus() -> None:
    """ATS boards do emit them. A future date is bad data, not a reason to rank
    something first."""
    assert recency(NOW + timedelta(days=30), NOW, make_settings()) == Decimal("1.0000")


@pytest.mark.parametrize(
    ("hard", "gate"),
    [("1.00", "1.00"), ("0.60", "1.00"), ("0.59", "0.85"), ("0.40", "0.85"), ("0.39", "0.65")],
)
def test_the_hard_gate_bands(hard: str, gate: str) -> None:
    assert hard_gate(Decimal(hard), make_settings()) == Decimal(gate)


def test_the_gate_is_what_makes_hard_coverage_dominant() -> None:
    """Without it, excellent nice-to-have coverage climbs the ranking on a role
    whose actual requirements the variant does not meet — the failure mode that
    produces confident, wasted applications."""
    config = make_settings()
    weak_hard = composite(
        Decimal("0.20"),
        Decimal("1.00"),
        tier=CompanyTier.STRONG,
        posted_at=NOW,
        now=NOW,
        settings=config,
    )
    strong_hard = composite(
        Decimal("0.70"),
        Decimal("0.20"),
        tier=CompanyTier.STRONG,
        posted_at=NOW,
        now=NOW,
        settings=config,
    )
    assert strong_hard.composite_score > weak_hard.composite_score


def test_a_dream_tier_perfect_match_is_clamped_to_one_hundred() -> None:
    """`composite_score` is NUMERIC(5,2) and 1.10 x 100 does not fit it."""
    result = composite(
        Decimal("1"),
        Decimal("1"),
        tier=CompanyTier.DREAM,
        posted_at=NOW,
        now=NOW,
        settings=make_settings(),
    )
    assert result.composite_score == Decimal("100.00")


def test_tier_is_a_ten_percent_band() -> None:
    """Tier breaks a tie between comparable roles. It must never let a
    dream-tier role the operator cannot do outrank a strong-tier role they can."""
    config = make_settings()
    assert tier_weight(CompanyTier.DREAM, config) - tier_weight(
        CompanyTier.VOLUME, config
    ) == Decimal("0.20")


# --------------------------------------------------------------------------
# §6.1 — ranking
# --------------------------------------------------------------------------


def contender(
    *, variant_id: int, composite_score: str, hard: str, hard_missing: int = 0
) -> VariantScore:
    """A VariantScore built directly, so `rank` is tested in isolation.

    Driving these through `score_variant` was the first attempt and it does not
    work: producing two variants whose composites differ by under a point, with
    *different* hard coverage, needs the requirement weights tuned to four
    decimal places — a fixture nobody could read, testing the arithmetic rather
    than the tie-break it is supposed to be about.
    """
    return VariantScore(
        variant=variant(variant_id=variant_id, key=f"v{variant_id}"),
        outcomes=(),
        hard=tuple(
            outcome_at(CoverageLevel.MISSING, "1.00", ordinal=n) for n in range(hard_missing)
        ),
        nice=(),
        hard_coverage=Decimal(hard),
        nice_coverage=Decimal("0"),
        result=Composite(
            coverage_pct=Decimal(composite_score),
            composite_score=Decimal(composite_score),
            tier_weight=Decimal("1.00"),
            recency=Decimal("1.0000"),
            hard_gate=Decimal("1.00"),
        ),
    )


def test_the_highest_composite_wins() -> None:
    low = contender(variant_id=1, composite_score="41.00", hard="1.00")
    high = contender(variant_id=2, composite_score="72.00", hard="0.60")
    assert rank([low, high]).variant.id == 2


def test_a_variant_outside_the_tie_band_cannot_win_on_hard_coverage() -> None:
    """The band is what stops rule 2 becoming "hard coverage decides everything".
    A 30-point deficit is a real difference, not a rounding artefact."""
    behind = contender(variant_id=1, composite_score="42.00", hard="1.00")
    ahead = contender(variant_id=2, composite_score="72.00", hard="0.60")
    assert rank([behind, ahead]).variant.id == 2


def test_inside_the_tie_band_hard_coverage_outranks_the_composite() -> None:
    """Rule 2, and the reason the composite is not re-consulted inside the band.

    The first implementation sorted contenders by composite first, which meant
    this rule could never fire — a variant 0.4 behind was already ranked below,
    whatever its hard coverage. Being inside the band has to make the composite
    *stop mattering*, because a gap under a point is exactly the rounding
    artefact the tie-break exists to overrule.
    """
    strong_hard = contender(variant_id=1, composite_score="71.60", hard="0.90")
    strong_nice = contender(variant_id=2, composite_score="72.00", hard="0.55")
    assert strong_nice.result.composite_score - strong_hard.result.composite_score <= TIE_BAND
    assert rank([strong_hard, strong_nice]).variant.id == 1


def test_fewer_missing_hard_requirements_breaks_a_coverage_tie() -> None:
    """Rule 3. Equal weighted coverage can hide a different number of flat
    rejections at the filter stage, and each of those is a screen the
    application does not survive."""
    many = contender(variant_id=1, composite_score="72.00", hard="0.60", hard_missing=3)
    few = contender(variant_id=2, composite_score="72.00", hard="0.60", hard_missing=1)
    assert rank([many, few]).variant.id == 2


def test_the_operators_default_variant_breaks_a_remaining_tie() -> None:
    first = contender(variant_id=1, composite_score="72.00", hard="0.60")
    second = contender(variant_id=2, composite_score="72.00", hard="0.60")
    assert rank([first, second], default_variant_id=2).variant.id == 2
    assert rank([first, second]).variant.id == 1, "with no default, the lowest ID wins"


def test_ranking_is_total_so_a_rescore_reproduces_the_winner() -> None:
    """Without a total order, §11's A/B comparison measures the shuffle instead
    of the change."""
    scores = [contender(variant_id=n, composite_score="72.00", hard="0.60") for n in (3, 1, 2)]
    assert rank(scores).variant.id == 1
    assert rank(list(reversed(scores))).variant.id == 1


def test_ranking_nothing_is_an_error_not_a_none() -> None:
    with pytest.raises(ValueError, match="nothing to rank"):
        rank([])


# --------------------------------------------------------------------------
# §13.2 — failure modes
# --------------------------------------------------------------------------


def test_a_posting_with_no_hard_requirements_is_flagged_not_scored() -> None:
    """The single most dangerous default in the formula.

    An empty hard bucket returns H = 1 by §4.4's rule — correct for the nice
    bucket, catastrophic for this one, because it would rank a job description
    the extractor failed on above every real match.
    """
    result = plan_posting(
        [requirement(kind="nice", text="Rust", skill="rust")],
        [variant()],
        posting_id="01J",
        tier=CompanyTier.STRONG,
        posted_at=NOW,
        now=NOW,
        settings=make_settings(),
    )
    assert result.winner is None
    assert result.note is not None
    assert "manual review" in result.note


def test_one_variant_failing_does_not_lose_the_others() -> None:
    """Fail open on enrichment. Five good rows and one logged failure beat no
    recommendation at all."""

    class Exploding(VariantView):
        def bullets_for(self, token: str) -> tuple[Bullet, ...]:
            raise RuntimeError("boom")

    good = variant(variant_id=1)
    bad = Exploding(id=2, key="broken", skill_set=frozenset({"python"}), bullets=())
    result = plan_posting(
        [requirement()],
        [good, bad],
        posting_id="01J",
        tier=CompanyTier.STRONG,
        posted_at=NOW,
        now=NOW,
        settings=make_settings(),
    )
    assert result.winner is not None
    assert result.winner.variant.id == 1
    assert result.skipped == ("broken",)


def test_a_condition_never_reaches_the_score() -> None:
    """ "Four ten-hour shifts covering weekends" is something to read before
    applying, not a gap in the candidate."""
    result = score_variant(
        [
            requirement(kind="hard", text="Python", skill="python"),
            requirement(row_id=2, kind="condition", text="Weekend shifts", skill=None, ordinal=1),
        ],
        variant(),
        tier=CompanyTier.STRONG,
        posted_at=NOW,
        now=NOW,
        settings=make_settings(),
    )
    assert len(result.outcomes) == 1
    assert build_gaps(result.outcomes) == []


def test_a_tool_requirement_lands_in_the_nice_bucket() -> None:
    """MATCH_SCORING.md §4.4. A named tool is a demand on the candidate — a
    cheap one to close, and exactly the nameable gap the gap list is for."""
    result = score_variant(
        [
            requirement(kind="hard", text="Python", skill="python"),
            requirement(row_id=2, kind="tool", text="Terraform", skill="terraform", ordinal=1),
        ],
        variant(),
        tier=CompanyTier.STRONG,
        posted_at=NOW,
        now=NOW,
        settings=make_settings(),
    )
    assert len(result.hard) == 1
    assert len(result.nice) == 1
    assert result.nice[0].requirement.kind == RequirementKind.TOOL.value


# --------------------------------------------------------------------------
# §8 — gaps
# --------------------------------------------------------------------------


def test_gaps_are_sorted_worst_first() -> None:
    """The first entry is always the biggest reason not to apply."""
    outcomes = [
        outcome_at(CoverageLevel.PARTIAL, "1.00", kind="hard", ordinal=0),
        outcome_at(CoverageLevel.MISSING, "1.00", kind="nice", ordinal=1),
        outcome_at(CoverageLevel.MISSING, "1.00", kind="hard", ordinal=2),
    ]
    assert [(gap["kind"], gap["level"]) for gap in build_gaps(outcomes)] == [
        ("hard", "missing"),
        ("hard", "partial"),
        ("nice", "missing"),
    ]


def test_a_met_requirement_is_not_a_gap() -> None:
    assert build_gaps([outcome_at(CoverageLevel.MET, "1.00")]) == []


def test_blocking_gaps_are_the_hard_ones_that_are_missing() -> None:
    """Three of these is a skip, and skipping costs one click and no tokens.
    The number that says "apply" is not the score, it is the absence of these."""
    gaps = build_gaps(
        [
            outcome_at(CoverageLevel.MISSING, "1.00", kind="hard", ordinal=0),
            outcome_at(CoverageLevel.PARTIAL, "1.00", kind="hard", ordinal=1),
            outcome_at(CoverageLevel.MISSING, "1.00", kind="nice", ordinal=2),
        ]
    )
    assert len(blocking_gaps(gaps)) == 1


def test_evidence_carries_the_bullet_and_its_claims() -> None:
    outcome = level_for(
        requirement(),
        variant(bullets=(("b.0", "Shipped 312 tests.", ("python",), ("pqbot.tests",)),)),
    )
    rows = build_evidence([outcome])
    assert rows[0]["bullet_ref"] == "b.0"
    assert rows[0]["claim_keys"] == ["pqbot.tests"]
    assert rows[0]["level"] == "met"


# --------------------------------------------------------------------------
# §11.2 — provenance
# --------------------------------------------------------------------------


def test_the_score_version_names_the_formula_and_the_adjacency() -> None:
    """An adjacency edit changes every score it touches. A row citing only the
    formula version could not be told apart from one scored before that edit —
    the same reasoning that puts the vocabulary in `requirement.prompt_version`."""
    version = score_version(make_settings(), get_adjacency())
    assert version.startswith("score.v")
    assert "+adjacency." in version
