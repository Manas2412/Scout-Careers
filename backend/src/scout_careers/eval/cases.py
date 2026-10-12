"""The golden set: forty hand-labelled job descriptions. TEST_PLAN.md §6.1.

Loading and validation only — no database, no model. The schema is the part of
this that matters most, because it is what the operator fills in by hand, once,
and the eval is worth exactly as much as those labels.

**The labels are written before the measurement, not after.** A golden set
quietly adjusted to match current behaviour is a mirror with a test runner
attached (AI_ARCHITECTURE.md §10.1). Two things here defend that:

- ``expected_recommended_variant`` is "the one an operator would defend in
  conversation, not the one the current formula produces" (TEST_PLAN.md §6.2).
  The straddle slice exists precisely because ``combined`` *should* often win
  and frequently does not.
- :attr:`GoldenCase.revision_reason` is required the moment a case is edited
  after first being labelled. Revising is allowed; revising silently is not.

The JD text lives in a sibling ``.txt`` rather than inside the YAML. Job
descriptions run to several thousand characters with their own quoting, and a
YAML block scalar of that size is unreviewable in a pull request — and the
adversarial slice deliberately contains text designed to confuse a parser.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scout_careers.common.errors import ConfigError


class Slice(StrEnum):
    """The eight slices and nothing else. TEST_PLAN.md §6.1.

    A closed set because the counts are specified: a case filed under a slice
    nobody counts is a case that silently stops balancing the set.
    """

    AI_PRODUCT = "ai_product"
    BACKEND = "backend"
    CONSULTING = "consulting"
    MISMATCH = "mismatch"
    STRADDLE = "straddle"
    VERBOSE = "verbose"
    TERSE = "terse"
    ADVERSARIAL = "adversarial"


#: How many cases each slice must hold for the set to be complete, and why.
#: Deliberately skewed toward the hard cases rather than sampled evenly.
SLICE_TARGETS: dict[Slice, int] = {
    Slice.AI_PRODUCT: 6,
    Slice.BACKEND: 6,
    Slice.CONSULTING: 6,
    Slice.MISMATCH: 5,
    Slice.STRADDLE: 5,
    Slice.VERBOSE: 4,
    Slice.TERSE: 4,
    Slice.ADVERSARIAL: 4,
}

GOLDEN_TOTAL = sum(SLICE_TARGETS.values())


class LabelledRequirement(BaseModel):
    """One requirement as the operator read it out of the posting."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    kind: str
    #: ``None`` is a real answer: a requirement the controlled vocabulary has no
    #: token for. Labelling it as such is what makes normalisation accuracy
    #: measurable — the metric is computed over labelled requirements that DO
    #: carry a token, so a null here excludes the row rather than failing it.
    normalised_skill: str | None = None
    weight: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0


class CaseSource(BaseModel):
    """Where the posting came from, so a label can be traced to a real ad."""

    model_config = ConfigDict(extra="forbid")

    company: str = Field(min_length=1)
    captured_at: str = Field(min_length=1)
    #: The `job_posting.id` this was drawn from, when it came out of the live
    #: corpus. Lets a surprising eval result be chased back to the row.
    posting_id: str | None = None


class CaseLabels(BaseModel):
    """What the operator says the right answer is."""

    model_config = ConfigDict(extra="forbid")

    requirements: list[LabelledRequirement] = Field(default_factory=list)

    #: The variant the operator would actually send. ``None`` means "send
    #: nothing" and is the correct label for every mismatch case.
    expected_recommended_variant: str | None = None
    expected_top_2: list[str] = Field(default_factory=list)
    expected_gaps_include: list[str] = Field(default_factory=list)

    #: ``allowed`` or ``blocked``. The mismatch slice is ``blocked``: those five
    #: cases are the ones that catch a scorer which has become too generous.
    expected_generation: str = "allowed"

    #: Per-requirement coverage the operator assigned, keyed by variant then by
    #: requirement text — the input to Cohen's κ. Optional: a case is useful for
    #: extraction metrics before anyone has sat down to label coverage.
    expected_coverage: dict[str, dict[str, str]] = Field(default_factory=dict)

    #: The operator's own ranking of the variants for this JD, best first. The
    #: input to Spearman ρ. Also optional, for the same reason.
    expected_variant_order: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.expected_generation not in {"allowed", "blocked"}:
            raise ValueError(
                f"expected_generation must be allowed|blocked, got {self.expected_generation!r}"
            )
        if (
            self.expected_recommended_variant
            and self.expected_top_2
            and self.expected_recommended_variant not in self.expected_top_2
        ):
            raise ValueError(
                f"expected_recommended_variant {self.expected_recommended_variant!r} "
                "is not in expected_top_2"
            )
        return self


class GoldenCase(BaseModel):
    """One labelled job description."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    slice: Slice
    source: CaseSource
    jd_text_file: str = Field(min_length=1)
    labels: CaseLabels

    labelled_by: str = "operator"
    labelled_at: str | None = None
    #: Required once a case has been revised. "Labelling is done once, by the
    #: operator, and revised only with a recorded reason" (TEST_PLAN.md §6.1) —
    #: this is that reason, and its absence on a revision is a load failure.
    revision_reason: str | None = None

    #: Filled by the loader, not by the file.
    jd_text: str = ""

    @model_validator(mode="after")
    def _adversarial_cases_must_assert_something(self) -> Self:
        """An adversarial case with no expectation tests nothing.

        The injection slice passes when the output is unaffected, so the case
        has to say what unaffected looks like.
        """
        if self.slice is Slice.ADVERSARIAL and not self.labels.requirements:
            raise ValueError(
                f"{self.id}: an adversarial case must label the requirements it expects"
            )
        return self

    @model_validator(mode="after")
    def _mismatches_recommend_nothing(self) -> Self:
        if self.slice is Slice.MISMATCH:
            if self.labels.expected_recommended_variant is not None:
                raise ValueError(f"{self.id}: a mismatch case must recommend nothing")
            if self.labels.expected_generation != "blocked":
                raise ValueError(f"{self.id}: a mismatch case must expect generation blocked")
        return self


def load_case(path: Path) -> GoldenCase:
    """Read one case and its JD text.

    Raises:
        ConfigError: The YAML is invalid, or the ``.txt`` it names is absent.
            Absent text is a hard failure rather than an empty string: a case
            that silently evaluates against nothing would pass every extraction
            metric vacuously.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        case = GoldenCase.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"{path.name} is not a valid golden case: {exc}") from exc

    text_path = path.parent / case.jd_text_file
    if not text_path.exists():
        raise ConfigError(f"{path.name} names {case.jd_text_file}, which does not exist")
    return case.model_copy(update={"jd_text": text_path.read_text(encoding="utf-8")})


def load_cases(directory: Path) -> tuple[GoldenCase, ...]:
    """Every case in a directory, sorted by id, with duplicate ids refused."""
    if not directory.is_dir():
        return ()
    cases = tuple(sorted((load_case(p) for p in directory.glob("*.yaml")), key=lambda c: c.id))
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ConfigError(f"duplicate golden case id: {case.id}")
        seen.add(case.id)
    return cases


def slice_counts(cases: tuple[GoldenCase, ...]) -> dict[Slice, int]:
    counts = dict.fromkeys(SLICE_TARGETS, 0)
    for case in cases:
        counts[case.slice] += 1
    return counts


def completeness(cases: tuple[GoldenCase, ...]) -> Iterator[str]:
    """Which slices are short, and by how much.

    Reported rather than raised. The set is built over weeks of labelling, and
    a harness that refused to run until all forty existed would mean no
    measurement at all until the last one landed.
    """
    counts = slice_counts(cases)
    for name, target in SLICE_TARGETS.items():
        if counts[name] < target:
            yield f"{name.value}: {counts[name]}/{target}"


__all__ = [
    "GOLDEN_TOTAL",
    "SLICE_TARGETS",
    "CaseLabels",
    "CaseSource",
    "GoldenCase",
    "LabelledRequirement",
    "Slice",
    "completeness",
    "load_case",
    "load_cases",
    "slice_counts",
]
