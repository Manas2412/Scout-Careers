"""The golden-case schema, and the refusals that keep the labels honest.

The eval is worth exactly as much as these labels, so the schema's job is to
make the ways a case can quietly stop testing anything into load failures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scout_careers.common.errors import ConfigError
from scout_careers.eval.cases import (
    GOLDEN_TOTAL,
    SLICE_TARGETS,
    GoldenCase,
    Slice,
    completeness,
    load_case,
    load_cases,
    slice_counts,
)

MINIMAL = {
    "id": "001-example",
    "slice": "backend",
    "source": {"company": "Example", "captured_at": "2026-10-09"},
    "jd_text_file": "001-example.txt",
    "labels": {
        "requirements": [
            {"text": "Python in production", "kind": "hard", "normalised_skill": "python"},
        ],
        "expected_recommended_variant": "backend",
        "expected_top_2": ["backend", "combined"],
    },
}


def write(tmp: Path, data: dict, text: str = "A job description.") -> Path:
    path = tmp / f"{data['id']}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    (tmp / data["jd_text_file"]).write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The shape of the set
# --------------------------------------------------------------------------


def test_the_slice_targets_match_the_specification() -> None:
    """TEST_PLAN.md §6.1. Forty cases, deliberately skewed toward hard ones."""
    assert GOLDEN_TOTAL == 40
    assert SLICE_TARGETS[Slice.AI_PRODUCT] == 6
    assert SLICE_TARGETS[Slice.BACKEND] == 6
    assert SLICE_TARGETS[Slice.CONSULTING] == 6
    assert SLICE_TARGETS[Slice.MISMATCH] == 5
    assert SLICE_TARGETS[Slice.STRADDLE] == 5
    assert SLICE_TARGETS[Slice.VERBOSE] == 4
    assert SLICE_TARGETS[Slice.TERSE] == 4
    assert SLICE_TARGETS[Slice.ADVERSARIAL] == 4


def test_an_unknown_slice_is_refused() -> None:
    """A case filed under a slice nobody counts silently stops balancing the
    set — it is present, it runs, and no completeness report mentions it."""
    with pytest.raises(ValueError):
        GoldenCase.model_validate({**MINIMAL, "slice": "miscellaneous"})


def test_completeness_reports_what_is_short_rather_than_refusing() -> None:
    """The set is built over weeks. A harness that refused to run until all
    forty existed would mean no measurement at all until the last one landed."""
    assert len(list(completeness(()))) == len(SLICE_TARGETS)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_a_case_loads_with_its_jd_text(tmp_path: Path) -> None:
    case = load_case(write(tmp_path, MINIMAL, "Build backend services in Python."))
    assert case.id == "001-example"
    assert case.jd_text == "Build backend services in Python."
    assert case.labels.requirements[0].normalised_skill == "python"


def test_a_missing_jd_text_file_is_a_hard_failure(tmp_path: Path) -> None:
    """Not an empty string. A case evaluating against no text would pass every
    extraction metric vacuously while testing nothing at all."""
    path = tmp_path / "001-example.yaml"
    path.write_text(yaml.safe_dump(MINIMAL), encoding="utf-8")
    with pytest.raises(ConfigError, match="does not exist"):
        load_case(path)


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    """A typo'd key would otherwise be dropped in silence, and the label it
    carried would never be checked."""
    with pytest.raises(ConfigError):
        load_case(write(tmp_path, {**MINIMAL, "expected_varient": "backend"}))


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL)
    second = {**MINIMAL, "jd_text_file": "dup.txt"}
    (tmp_path / "dup.yaml").write_text(yaml.safe_dump(second), encoding="utf-8")
    (tmp_path / "dup.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate"):
        load_cases(tmp_path)


def test_an_absent_directory_loads_nothing_rather_than_failing(tmp_path: Path) -> None:
    assert load_cases(tmp_path / "nope") == ()


def test_cases_are_counted_by_slice(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL)
    assert slice_counts(load_cases(tmp_path))[Slice.BACKEND] == 1


# --------------------------------------------------------------------------
# The refusals that keep a slice meaningful
# --------------------------------------------------------------------------


def test_a_mismatch_case_must_recommend_nothing() -> None:
    """The five mismatch cases are the ones that catch a scorer which has
    become too generous. One labelled with a variant cannot do that."""
    with pytest.raises(ValueError, match="recommend nothing"):
        GoldenCase.model_validate(
            {
                **MINIMAL,
                "slice": "mismatch",
                "labels": {**MINIMAL["labels"], "expected_generation": "blocked"},
            }
        )


def test_a_mismatch_case_must_expect_generation_blocked() -> None:
    with pytest.raises(ValueError, match="generation blocked"):
        GoldenCase.model_validate(
            {
                **MINIMAL,
                "slice": "mismatch",
                "labels": {"requirements": [], "expected_recommended_variant": None},
            }
        )


def test_an_adversarial_case_must_state_what_unaffected_looks_like() -> None:
    """The injection slice passes when the output is unchanged, so a case with
    nothing labelled asserts nothing and would pass forever."""
    with pytest.raises(ValueError, match="label the requirements"):
        GoldenCase.model_validate(
            {
                **MINIMAL,
                "slice": "adversarial",
                "labels": {"requirements": [], "expected_recommended_variant": None},
            }
        )


def test_the_recommended_variant_must_be_inside_the_top_two() -> None:
    """Otherwise the two labels contradict each other and both metrics that
    read them are measuring against nonsense."""
    with pytest.raises(ValueError, match="not in expected_top_2"):
        GoldenCase.model_validate(
            {
                **MINIMAL,
                "labels": {
                    **MINIMAL["labels"],
                    "expected_recommended_variant": "consulting",
                    "expected_top_2": ["backend", "combined"],
                },
            }
        )


def test_expected_generation_takes_only_the_two_documented_values() -> None:
    with pytest.raises(ValueError, match="allowed\\|blocked"):
        GoldenCase.model_validate(
            {**MINIMAL, "labels": {**MINIMAL["labels"], "expected_generation": "maybe"}}
        )


def test_a_requirement_with_no_vocabulary_token_is_allowed() -> None:
    """`None` is a real label: the vocabulary genuinely has no token for some
    requirements. Normalisation accuracy is computed over the ones that DO
    carry a token, so a null excludes the row rather than failing it."""
    case = GoldenCase.model_validate(
        {
            **MINIMAL,
            "labels": {
                **MINIMAL["labels"],
                "requirements": [{"text": "Comfortable with ambiguity", "kind": "nice"}],
            },
        }
    )
    assert case.labels.requirements[0].normalised_skill is None
