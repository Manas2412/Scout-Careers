"""Per-bullet tags: what a resume line proves, and what backs its numbers.

``skill_set`` answers "can the operator claim this?". These answer "which
sentence proves it?" — which is what a generated document needs, because the
line the score rests on is the line that has to appear on the page.

The tags are hand-written. What is machine-checked is that every one *resolves*:
an unknown vocabulary token, or a claim key the operator has not approved, is a
load failure before anything reaches the table.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scout_careers.cli.seed import (
    DEFAULT_BULLET_TAGS_PATH,
    DEFAULT_CLAIMS_PATH,
    DEFAULT_VARIANTS_DIR,
    approved_claim_keys,
    check_bullet_tags,
    load_bullet_tags,
    load_variants,
)
from scout_careers.extract.variants import (
    apply_bullet_tags,
    bullet_ids,
    bullet_tag_problems,
    iter_bullets,
    skill_set_for,
    unproven_skills,
    untagged_bullets,
    variant_skill_set,
)
from scout_careers.extract.vocabulary import get_vocabulary

CONTENT: dict[str, Any] = {
    "experience": [
        {
            "blocks": [
                {
                    "bullets": [
                        {"id": "v.exp.0.0.0", "text": "Built a thing with 312 tests."},
                        {"id": "v.exp.0.0.1", "text": "Built another thing."},
                    ]
                }
            ]
        }
    ],
    "projects": [{"bullets": [{"id": "v.proj.0.0", "text": "Scaled a pipeline."}]}],
}

TAGS: dict[str, dict[str, list[str]]] = {
    "v.exp.0.0.0": {"skills": ["python", "testing"], "claims": ["pqbot.tests"]},
    "v.proj.0.0": {"skills": ["data_pipeline"], "claims": []},
}


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------


def test_tags_land_on_the_bullets_they_name() -> None:
    merged = apply_bullet_tags(CONTENT, TAGS)
    by_id = {bullet.id: bullet for bullet in iter_bullets(merged)}
    assert by_id["v.exp.0.0.0"].skills == ("python", "testing")
    assert by_id["v.exp.0.0.0"].claim_keys == ("pqbot.tests",)
    assert by_id["v.proj.0.0"].skills == ("data_pipeline",)


def test_an_untagged_bullet_is_left_alone() -> None:
    merged = apply_bullet_tags(CONTENT, TAGS)
    by_id = {bullet.id: bullet for bullet in iter_bullets(merged)}
    assert by_id["v.exp.0.0.1"].skills == ()
    assert untagged_bullets(merged) == ("v.exp.0.0.1",)


def test_merging_does_not_mutate_the_transcribed_content() -> None:
    """The JSON files are a transcription of the .docx and nothing else.

    ``variants-from-docx.py`` overwrites them wholesale on every resume edit,
    so a tag written back into them would be destroyed by an ordinary edit —
    silently, with the scores simply ceasing to rest on anything.
    """
    apply_bullet_tags(CONTENT, TAGS)
    original = CONTENT["experience"][0]["blocks"][0]["bullets"][0]
    assert "skills" not in original


def test_projects_are_walked_after_experience() -> None:
    """Document order, because it is the order a reader meets the evidence in."""
    assert [bullet.id for bullet in iter_bullets(CONTENT)] == [
        "v.exp.0.0.0",
        "v.exp.0.0.1",
        "v.proj.0.0",
    ]


# --------------------------------------------------------------------------
# What the gate refuses
# --------------------------------------------------------------------------


def test_a_skill_outside_the_vocabulary_is_refused() -> None:
    tags = {"v.exp.0.0.0": {"skills": ["kotlin"], "claims": []}}
    problems = bullet_tag_problems(apply_bullet_tags(CONTENT, tags), approved_claims=frozenset())
    assert any("kotlin" in problem for problem in problems)


def test_an_unknown_claim_key_is_refused() -> None:
    tags = {"v.exp.0.0.0": {"skills": [], "claims": ["pqbot.invented"]}}
    problems = bullet_tag_problems(
        apply_bullet_tags(CONTENT, tags), approved_claims={"pqbot.tests"}
    )
    assert any("pqbot.invented" in problem for problem in problems)


def test_a_rejected_claim_is_refused_exactly_like_an_unknown_one() -> None:
    """Downstream they are the same failure: a number with nothing behind it.

    The operator withdrew 19 claims rather than citing evidence for them. Those
    bullets keep their text and lose their backing — they degrade to ``partial``
    under MATCH_SCORING.md §4.2 — and citing one anyway must not be possible.
    """
    tags = {"v.exp.0.0.0": {"skills": [], "claims": ["pqbot.config_settings"]}}
    problems = bullet_tag_problems(
        apply_bullet_tags(CONTENT, tags), approved_claims={"pqbot.tests"}
    )
    assert problems and "not an approved claim" in problems[0]


def test_every_problem_is_reported_not_just_the_first() -> None:
    """A loader that stops at the first bad line turns one review pass into six."""
    tags = {
        "v.exp.0.0.0": {"skills": ["kotlin"], "claims": ["nope"]},
        "v.proj.0.0": {"skills": ["haskell"], "claims": []},
    }
    problems = bullet_tag_problems(apply_bullet_tags(CONTENT, tags), approved_claims=frozenset())
    assert len(problems) == 3


def test_a_tag_naming_no_bullet_is_refused(tmp_path: Path) -> None:
    """The failure most likely to actually happen.

    Bullet IDs are positional, so reordering a resume renumbers them. An
    orphaned tag ignored in silence would move its claims onto whichever line
    inherited the number.
    """
    from scout_careers.cli.seed import SeedVariant

    variant = SeedVariant(key="v", name="V", target="t", content=CONTENT)
    with pytest.raises(ValueError, match="names no bullet"):
        check_bullet_tags(
            [variant],
            {"v.exp.9.9.9": {"skills": [], "claims": []}},
            tmp_path / "absent.yaml",
        )


# --------------------------------------------------------------------------
# The shipped files
# --------------------------------------------------------------------------


def test_the_shipped_tags_resolve() -> None:
    """The real six variants, the real vocabulary, the real ledger.

    This is the test that matters: it is the one that fails when a claim is
    withdrawn, a vocabulary token is renamed, or a resume bullet is reordered.
    """
    if not DEFAULT_VARIANTS_DIR.is_dir() or not DEFAULT_BULLET_TAGS_PATH.exists():
        pytest.skip("no shipped variants or tags")
    check_bullet_tags(
        load_variants(DEFAULT_VARIANTS_DIR),
        load_bullet_tags(DEFAULT_BULLET_TAGS_PATH),
        DEFAULT_CLAIMS_PATH,
    )


def test_every_shipped_bullet_is_tagged() -> None:
    """Untagged is legal but inert, and none of these are meant to be.

    A bullet with no skills can never be selected as proof of a requirement, so
    an accidentally untagged line does not fail anything — it just quietly stops
    counting. That is precisely why it is asserted here rather than left to be
    noticed later.
    """
    if not DEFAULT_VARIANTS_DIR.is_dir() or not DEFAULT_BULLET_TAGS_PATH.exists():
        pytest.skip("no shipped variants or tags")
    tags = load_bullet_tags(DEFAULT_BULLET_TAGS_PATH)
    for variant in load_variants(DEFAULT_VARIANTS_DIR):
        merged = apply_bullet_tags(variant.content, tags)
        assert not untagged_bullets(merged), f"{variant.key} has untagged bullets"


def test_no_shipped_tag_is_orphaned() -> None:
    if not DEFAULT_VARIANTS_DIR.is_dir() or not DEFAULT_BULLET_TAGS_PATH.exists():
        pytest.skip("no shipped variants or tags")
    tags = load_bullet_tags(DEFAULT_BULLET_TAGS_PATH)
    known: set[str] = set()
    for variant in load_variants(DEFAULT_VARIANTS_DIR):
        known |= bullet_ids(variant.content)
    assert set(tags) <= known


def test_every_bullet_skill_reaches_the_skill_set() -> None:
    """The half of the coverage intersection a bullet tag has to land in.

    If a bullet proves ``testing`` and ``skill_set`` does not contain it, a
    "strong testing culture" requirement scores MISSING while a hand-verified
    sentence sits in the same document proving it. The union is what makes the
    tag mean anything.
    """
    if not DEFAULT_VARIANTS_DIR.is_dir() or not DEFAULT_BULLET_TAGS_PATH.exists():
        pytest.skip("no shipped variants or tags")
    vocabulary = get_vocabulary()
    tags = load_bullet_tags(DEFAULT_BULLET_TAGS_PATH)
    for variant in load_variants(DEFAULT_VARIANTS_DIR):
        merged = apply_bullet_tags(variant.content, tags)
        tokens = set(variant_skill_set(merged, vocabulary=vocabulary))
        for bullet in iter_bullets(merged):
            assert set(bullet.skills) <= tokens, bullet.id


def test_the_bullets_widen_every_variants_skill_set() -> None:
    """The finding that forced the union, asserted so it cannot silently revert.

    No resume writes "Testing" or "Ownership" under Skills; it writes "312
    backend tests green" and "sole engineer" and expects the reader to draw the
    conclusion. Deriving ``skill_set`` from the skills line alone lost 8 to 11
    such tokens per variant — every one of them a thing employers ask for by
    name, and every one of them provable from a line already on the page.
    """
    if not DEFAULT_VARIANTS_DIR.is_dir() or not DEFAULT_BULLET_TAGS_PATH.exists():
        pytest.skip("no shipped variants or tags")
    vocabulary = get_vocabulary()
    tags = load_bullet_tags(DEFAULT_BULLET_TAGS_PATH)
    for variant in load_variants(DEFAULT_VARIANTS_DIR):
        merged = apply_bullet_tags(variant.content, tags)
        line_only = set(skill_set_for(merged.get("skills") or [], vocabulary=vocabulary))
        union = set(variant_skill_set(merged, vocabulary=vocabulary))
        assert union > line_only, f"{variant.key} gains nothing from its bullets"


def test_the_union_is_sorted_and_deduplicated() -> None:
    """An unordered set written to a ``TEXT[]`` produces a different row every
    seed, and a diff that changes every time is a diff nobody reads."""
    merged = apply_bullet_tags(CONTENT, TAGS)
    tokens = variant_skill_set(merged)
    assert list(tokens) == sorted(set(tokens))


def test_a_skill_no_bullet_proves_is_reported_not_refused() -> None:
    """The skills line is itself the operator's claim, so an unproven token is
    honest — but it can score a requirement MET while leaving generation with no
    sentence to cite, which is worth saying out loud."""
    content: dict[str, Any] = {
        "skills": [{"label": "Languages", "items": ["Python", "Rust"]}],
        "experience": [{"blocks": [{"bullets": [{"id": "v.exp.0.0.0", "text": "Wrote Python."}]}]}],
    }
    merged = apply_bullet_tags(content, {"v.exp.0.0.0": {"skills": ["python"], "claims": []}})
    assert unproven_skills(merged) == ("rust",)
    assert not bullet_tag_problems(merged, approved_claims=frozenset())


def test_no_shipped_bullet_cites_a_withdrawn_claim() -> None:
    """Stated separately from `test_the_shipped_tags_resolve` because this is
    the rule the operator will trip: withdrawing a claim is a normal act, and it
    has to fail loudly here rather than quietly weaken a document."""
    if not DEFAULT_BULLET_TAGS_PATH.exists() or not DEFAULT_CLAIMS_PATH.exists():
        pytest.skip("no shipped tags or ledger")
    approved = approved_claim_keys(DEFAULT_CLAIMS_PATH)
    cited = {
        key for tag in load_bullet_tags(DEFAULT_BULLET_TAGS_PATH).values() for key in tag["claims"]
    }
    assert cited <= approved
    assert cited, "the ledger exists to be cited; no bullet citing anything is a bug"


def test_the_tag_file_refuses_an_unknown_field(tmp_path: Path) -> None:
    """``extra="forbid"``, like every other seed file here: `claim` for `claims`
    would otherwise be a tag silently dropped."""
    path = tmp_path / "bullet_tags.yaml"
    path.write_text(
        yaml.safe_dump({"bullets": {"v.exp.0.0.0": {"skills": [], "claim": []}}}),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="claim"):
        load_bullet_tags(path)


def test_an_absent_tag_file_is_allowed(tmp_path: Path) -> None:
    """A fresh checkout must still be able to seed. Untagged variants are inert,
    not invalid."""
    assert load_bullet_tags(tmp_path / "nothing.yaml") == {}
