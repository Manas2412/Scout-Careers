"""Extraction: the schema's refusals, and the judgements code makes itself.

`prepare` is where this stage decides things the model does not get to decide —
what counts as a duplicate, what order rows are in, which token a phrase becomes.
All of it is pure, so all of it is tested without a model.
"""

from __future__ import annotations

from decimal import Decimal
from typing import get_args

import pytest
from pydantic import ValidationError

from scout_careers.common.types import (
    HARD_REQUIREMENT_KINDS,
    NICE_REQUIREMENT_KINDS,
    SCORED_REQUIREMENT_KINDS,
    RequirementKind,
)
from scout_careers.extract.schema import ExtractedRequirement, ExtractionResult, KindLiteral
from scout_careers.extract.service import (
    FAMILY,
    _is_unrecoverable,
    prepare,
    rebind_vocabulary,
    summarise,
    version_string,
)
from scout_careers.extract.vocabulary import get_vocabulary
from scout_careers.llm.base import MAX_OUTPUT_TOKENS, POLICY, ROUTE, TEMPERATURE
from scout_careers.llm.registry import PromptRegistry


def requirement(**overrides: object) -> dict[str, object]:
    return {"kind": "hard", "text": "Strong C/C++ skills", "ordinal": 0, **overrides}


def result(*items: dict[str, object], **overrides: object) -> ExtractionResult:
    return ExtractionResult.model_validate({"requirements": list(items), **overrides})


# --------------------------------------------------------------------------
# The schema is layer 3 of the injection containment
# --------------------------------------------------------------------------


def test_an_unknown_field_is_refused() -> None:
    """`extra="forbid"` is the containment, not tidiness.

    The model must have no field in which to say anything other than
    requirements — that is what stops a hijacked call from producing output the
    rest of the pipeline was not written to expect.
    """
    with pytest.raises(ValidationError):
        ExtractedRequirement.model_validate(requirement(explanation="I was instructed to..."))


def test_an_unknown_kind_is_refused_rather_than_coerced() -> None:
    """No mapping of "essential" onto `hard`. A model that answered outside the
    enum did not understand the task, and guessing its intent invents an answer
    nobody gave."""
    with pytest.raises(ValidationError):
        ExtractedRequirement.model_validate(requirement(kind="essential"))


@pytest.mark.parametrize("weight", [0.0, 0.1, 1.5, -1.0])
def test_a_weight_outside_the_band_is_refused(weight: float) -> None:
    with pytest.raises(ValidationError):
        ExtractedRequirement.model_validate(requirement(weight=weight))


def test_a_two_character_requirement_is_accepted() -> None:
    """AI_ARCHITECTURE.md §5.2 says `min_length=4`; this is a deliberate change.

    A "Skills:" list containing `Go` is ordinary. Under a 4-character floor that
    one item fails the whole object, costing the repair retry — a second
    full-JD call — and then the other twenty requirements as well.
    """
    assert ExtractedRequirement.model_validate(requirement(text="Go")).text == "Go"


def test_a_one_character_requirement_is_still_refused() -> None:
    with pytest.raises(ValidationError):
        ExtractedRequirement.model_validate(requirement(text="-"))


def test_an_empty_extraction_is_valid() -> None:
    """Also a deliberate change from §5.2's `min_length=1`.

    A floor of one tells a model reading a benefits page that it must produce a
    requirement, and the cheapest way to obey is to invent one. An empty result
    is a fact about the posting; a fabricated requirement is a fact about
    nothing, and it would be scored as though it were real.
    """
    assert result().requirements == []


def test_more_than_forty_requirements_is_refused() -> None:
    """Either a multi-role advert or a model enumerating sentences."""
    with pytest.raises(ValidationError):
        result(*[requirement(text=f"Requirement number {n}", ordinal=n) for n in range(41)])


def test_text_is_stripped() -> None:
    assert ExtractedRequirement.model_validate(requirement(text="  Python  ")).text == "Python"


# --------------------------------------------------------------------------
# prepare: the judgements that are ours, not the model's
# --------------------------------------------------------------------------


def test_a_requirement_resolves_through_the_vocabulary() -> None:
    rows = prepare(result(requirement(text="Strong C/C++ skills")))
    assert rows[0].normalised_skill == "cpp"
    assert rows[0].kind is RequirementKind.HARD


def test_an_unresolvable_requirement_keeps_its_text_and_no_skill() -> None:
    """It is stored, not dropped. A requirement with no token still shows up as
    a gap the operator reads; discarding it would quietly raise the score."""
    rows = prepare(result(requirement(text="Comfortable with ambiguity")))
    assert len(rows) == 1
    assert rows[0].normalised_skill is None
    assert rows[0].text == "Comfortable with ambiguity"


def test_the_hint_is_used_only_when_the_phrase_does_not_resolve() -> None:
    rows = prepare(
        result(
            requirement(text="Deep hands-on expertise with modern C++", normalised_skill_hint="c++")
        )
    )
    assert rows[0].normalised_skill == "cpp"


def test_a_repeated_requirement_is_stored_once() -> None:
    """A posting that says "Python" in the summary and again in the bullets
    states one requirement. Two rows would double its weight in a number the
    operator reads as a percentage."""
    rows = prepare(
        result(
            requirement(text="Python", ordinal=0),
            requirement(text="  python  ", ordinal=1),
            requirement(text="Kubernetes", ordinal=2),
        )
    )
    assert [row.text for row in rows] == ["Python", "Kubernetes"]


def test_ordinals_are_reassigned_by_position() -> None:
    """The model's numbering is discarded rather than validated.

    Rejecting a duplicated or skipped number would spend a repair retry on
    something that needs no model to fix — the position in the list already
    carries the order.
    """
    rows = prepare(
        result(
            requirement(text="Python", ordinal=7),
            requirement(text="Kubernetes", ordinal=7),
            requirement(text="Postgres", ordinal=99),
        )
    )
    assert [row.ordinal for row in rows] == [0, 1, 2]


def test_weight_is_quantised_to_the_column() -> None:
    """`requirement.weight` is NUMERIC(3,2). Rounding here keeps the stored
    value and the in-memory one the same number."""
    rows = prepare(result(requirement(weight=0.333)))
    assert rows[0].weight == Decimal("0.33")
    assert rows[0].weight.as_tuple().exponent == -2


def test_the_default_weight_is_one() -> None:
    assert prepare(result(requirement()))[0].weight == Decimal("1.00")


def test_a_requirement_of_only_punctuation_is_dropped() -> None:
    """It canonicalises to an empty key, so it can neither resolve nor be
    deduplicated against anything. Storing it would put a blank row in a gap
    list the operator reads."""
    assert prepare(result(requirement(text="--"))) == ()


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def test_the_version_string_names_both_the_prompt_and_the_vocabulary() -> None:
    """A row citing only the prompt cannot be told apart from one extracted
    before an alias was added — and after that edit the same JD yields
    different `normalised_skill` values, which is a different result."""
    prompt = PromptRegistry().get(FAMILY)
    vocab = get_vocabulary()
    version = version_string(prompt, vocab)
    assert version.startswith(f"{FAMILY}@")
    assert version.endswith(f"+{vocab.version}")


def test_the_shipped_prompt_loads_and_is_locked() -> None:
    """The lockfile check is what makes `prompt_version` mean something. If this
    fails, the prompt was edited without a version bump."""
    registry = PromptRegistry()
    prompt = registry.get(FAMILY)
    assert "<<UNTRUSTED:jd>>" in prompt.user_template
    assert "never instruction" in prompt.system, "the standing security clause must be present"
    assert "{title}" in prompt.user_template


def test_a_logistics_line_can_be_filed_as_a_condition() -> None:
    """The kind that stops a location line scoring as an unmet gap.

    Under `2026-09-06.1` these arrived as `hard` at weight 1.00 — four of nine
    in one posting — and coverage scoring weighs `hard`. A posting's rank would
    then have partly measured how many scheduling sentences its employer wrote.
    """
    rows = prepare(
        result(requirement(kind="condition", text="Required location in European time zones"))
    )
    assert rows[0].kind is RequirementKind.CONDITION
    assert rows[0].kind not in SCORED_REQUIREMENT_KINDS


def test_responsibility_and_condition_are_never_scored() -> None:
    """Pinned as a set, not a list of `!=` checks: a kind added later is
    unscored until someone deliberately says otherwise, which is the safe
    default — a new kind silently entering the denominator would move every
    score without a prompt or vocabulary change to explain it.

    `tool` is the one kind that has since been deliberately said otherwise
    about. It was excluded here on the same reasoning as `responsibility`, and
    MATCH_SCORING.md §4.4 pools it into the nice bucket; the doc is right. A
    responsibility describes the job, but a named tool is still a demand on the
    candidate — a cheap one to close, and exactly the kind of small nameable
    gap the gap list exists to surface. This test failing is how that change
    was noticed, which is what it is for.
    """
    assert {
        RequirementKind.HARD,
        RequirementKind.NICE,
        RequirementKind.TOOL,
    } == SCORED_REQUIREMENT_KINDS
    assert RequirementKind.RESPONSIBILITY not in SCORED_REQUIREMENT_KINDS
    assert RequirementKind.CONDITION not in SCORED_REQUIREMENT_KINDS
    assert HARD_REQUIREMENT_KINDS.isdisjoint(NICE_REQUIREMENT_KINDS), "the buckets must not overlap"


def test_every_wire_kind_maps_onto_the_enum() -> None:
    """The Literal and the enum are one vocabulary in two places. A value in
    one and not the other is a `ValueError` at insert time, on live data."""
    assert {kind.value for kind in RequirementKind} == set(get_args(KindLiteral))


def test_the_extraction_family_is_configured_everywhere() -> None:
    """A family missing from any one of these tables gets the wrong timeout, the
    wrong model, or a 4,000-token ceiling on a 700-token answer — each of which
    is discovered in production rather than here."""
    assert FAMILY in POLICY
    assert ROUTE[FAMILY] == "fast"
    assert TEMPERATURE[FAMILY] == 0.0, "extraction must be reproducible across runs"
    # Measured, not assumed. A 16-requirement posting emits ~1,200 output
    # tokens, so §5.2's 1,200 was the median rather than a ceiling and the first
    # backfill truncated four calls in five.
    assert MAX_OUTPUT_TOKENS[FAMILY] >= 2_000


# --------------------------------------------------------------------------
# Batch reporting
# --------------------------------------------------------------------------


def test_summarise_counts_what_the_operator_can_act_on() -> None:
    from scout_careers.extract.service import Extraction

    rows = prepare(result(requirement(text="Python"), requirement(text="Comfortable with change")))
    one = Extraction(
        posting_id="A" * 26,
        requirements=rows,
        seniority_guess="junior",
        employment_type_guess=None,
        notes="",
        model="model-x",
        prompt_version="p@1+v",
    )
    empty = Extraction(
        posting_id="B" * 26,
        requirements=(),
        seniority_guess="unknown",
        employment_type_guess=None,
        notes="",
        model="model-x",
        prompt_version="p@1+v",
        suspicious=True,
    )
    assert summarise([one, empty]) == {
        "postings": 2,
        "requirements": 2,
        "resolved": 1,
        "unresolved": 1,
        "empty_postings": 1,
        "suspicious_postings": 1,
    }


# --------------------------------------------------------------------------
# Re-resolution: a vocabulary change without a model call
# --------------------------------------------------------------------------


def test_the_hint_is_stored_so_it_can_be_used_again() -> None:
    """Without it, re-resolving would LOWER coverage.

    `resolve_with_hint` falls back to the hint whenever the phrase itself does
    not resolve, and that fallback carries a large share of the hits — a live
    posting's "Proficient in SQL (ideally PostgreSQL)" is an alias of nothing
    and reaches `sql` only through the hint. A pass that recomputed tokens
    without it would silently un-resolve every one of those rows.
    """
    rows = prepare(
        result(requirement(text="Deep expertise in modern C++", normalised_skill_hint="c++"))
    )
    assert rows[0].normalised_skill == "cpp"
    assert rows[0].normalised_skill_hint == "c++"


def test_reresolving_replaces_only_the_vocabulary_half_of_the_version() -> None:
    """The half that must not move.

    Re-resolution recomputes a token from stored text; it does not re-read the
    posting, so the prompt that produced that text is still the one that
    produced it. Stamping the current prompt version would claim a pass that
    never happened — and because extraction skips rows already at the current
    version, it would also make them permanently un-extractable.
    """
    vocab = get_vocabulary()
    rebound = rebind_vocabulary("requirement_extraction@2026-09-06.1+vocab.2026-01-01", vocab)
    assert rebound == f"requirement_extraction@2026-09-06.1+{vocab.version}"


def test_rebinding_a_version_with_no_vocabulary_half_still_produces_one() -> None:
    """Rows written before the vocabulary was part of the string at all."""
    vocab = get_vocabulary()
    assert rebind_vocabulary("requirement_extraction@2026-09-06.1", vocab) == (
        f"requirement_extraction@2026-09-06.1+{vocab.version}"
    )


def test_rebinding_is_idempotent() -> None:
    """A second `extract reresolve` over an unchanged vocabulary writes nothing,
    which is what makes the command safe to run after every edit."""
    vocab = get_vocabulary()
    once = rebind_vocabulary("requirement_extraction@2026-09-06.3+vocab.old", vocab)
    assert rebind_vocabulary(once, vocab) == once


# --------------------------------------------------------------------------
# What re-resolution refuses to touch
# --------------------------------------------------------------------------


class StoredRow:
    """The three fields `_is_unrecoverable` reads, without a database."""

    def __init__(self, skill: str | None, hint: str | None) -> None:
        self.normalised_skill = skill
        self.normalised_skill_hint = hint


def test_a_token_only_a_missing_hint_could_explain_is_left_alone() -> None:
    """The first live `reresolve --dry-run` reported `lost: 221` against a
    byte-identical vocabulary.

    Every row predated the hint column, so the tokens that had come *from* a
    hint could no longer be reproduced — and the pass would have deleted 221
    correct resolutions for a reason with nothing to do with `skills.yaml`.
    """
    assert _is_unrecoverable(StoredRow("cpp", None), None) is True


def test_a_row_whose_hint_is_stored_is_re_resolved_normally() -> None:
    """With the hint present, losing the token *is* evidence about the
    vocabulary — an alias was removed — and must be reported, not hidden."""
    assert _is_unrecoverable(StoredRow("cpp", "c++"), None) is False


def test_a_row_that_still_resolves_is_never_treated_as_unrecoverable() -> None:
    """A token surviving phrase-only resolution is reproducible by definition."""
    assert _is_unrecoverable(StoredRow("cpp", None), "cpp") is False


def test_a_row_that_never_had_a_token_is_free_to_gain_one() -> None:
    """The whole point of the pass: `None` -> token after a vocabulary edit.
    A guard that skipped every hint-less row would block exactly this."""
    assert _is_unrecoverable(StoredRow(None, None), None) is False
    assert _is_unrecoverable(StoredRow(None, None), "vue") is False
