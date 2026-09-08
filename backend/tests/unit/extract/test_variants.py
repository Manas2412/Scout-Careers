"""Deriving ``resume_variant.skill_set`` from a resume's skills lines.

This is the supply half of the coverage intersection. Everything here is about
it being *derived* — a hand-written list drifts from the resume the moment the
resume is edited, and the drift is invisible: the score simply becomes wrong
about what the operator can claim.
"""

from __future__ import annotations

from scout_careers.extract.variants import (
    resolve_item,
    skill_set_for,
    split_skills,
    unresolved_items,
)
from scout_careers.extract.vocabulary import get_vocabulary

AWS_LINE = "AWS (ECS Fargate, RDS, ECR, ALB + WAF, S3, Secrets Manager, IAM / OIDC)"


def test_a_bracketed_list_is_one_skill_not_its_fragments() -> None:
    """The bug this splitter exists to not have.

    The first version split on every separator, so the slash inside "IAM / OIDC"
    shredded the line and the backend variant lost `aws` entirely — the token
    was absent from the skill set of a resume whose cloud section is nothing but
    AWS services.
    """
    assert resolve_item(AWS_LINE, get_vocabulary()) == {"aws"}


def test_the_whole_item_is_tried_before_its_parts() -> None:
    """The most specific reading is the one most likely to be a real alias.

    "hybrid retrieval (RRF)" should resolve as itself, not by falling through to
    whatever "RRF" happens to mean on its own.
    """
    assert next(iter(split_skills("hybrid retrieval (RRF)"))) == "hybrid retrieval (RRF)"


def test_separators_outside_brackets_still_split() -> None:
    parts = list(split_skills("Docker; Azure (App Service, ACR)"))
    assert "Docker" in parts
    assert "Azure" in parts


def test_a_version_suffix_is_dropped_as_a_second_attempt() -> None:
    """ "Next.js 14" is the same skill as "Next.js"; a resume writes the version
    and a vocabulary does not carry every one."""
    assert resolve_item("Next.js 14", get_vocabulary()) == {"nextjs"}


def test_a_deliberately_versioned_alias_still_wins_on_its_own_terms() -> None:
    """Stripping runs only after the fuller forms fail, so an alias that names a
    version is never bypassed by its own stripped form."""
    assert resolve_item("Java 17", get_vocabulary()) == {"java"}


def test_an_item_naming_nothing_in_the_vocabulary_yields_nothing() -> None:
    """Silence is the honest answer for a skill the vocabulary cannot express.

    Inventing a token would put something in `skill_set` that no requirement can
    ever match, which reads as coverage and is not.

    `Bhashini` is the example on purpose: it is a real line in the operator's
    resume, deliberately left out of `vocab.2026-09-07.2` because nothing in the
    corpus asks for it (the comment above `orm` in `skills.yaml` records that
    decision). If it ever earns a token this test breaks — which is correct, and
    the fix is to pick another supply-only skill, not to delete the test.

    This example replaced `Pydantic v2`, which stopped naming nothing the moment
    `pydantic` was added.
    """
    assert resolve_item("Bhashini", get_vocabulary()) == set()


def test_the_skill_set_is_sorted_and_unique() -> None:
    """An unordered set written to a TEXT[] produces a different row every seed,
    and a diff that changes every time is a diff nobody reads."""
    groups = [
        {"label": "Cloud", "items": [AWS_LINE, "Azure (App Service)"]},
        {"label": "Languages", "items": ["Python", "python", "TypeScript"]},
    ]
    tokens = skill_set_for(groups, vocabulary=get_vocabulary())
    assert tokens == tuple(sorted(set(tokens)))
    assert {"aws", "azure", "python", "typescript"} <= set(tokens)


def test_unresolved_items_are_reported_rather_than_dropped_silently() -> None:
    """The supply half of the proposal queue: something the operator can claim
    and the vocabulary cannot express is unmatchable however often it is asked
    for."""
    groups = [{"label": "Backend", "items": ["Python", "Bhashini", "Fastify"]}]
    missing = unresolved_items(groups, vocabulary=get_vocabulary())
    assert "Bhashini" in missing
    assert "Python" not in missing
    assert "Fastify" not in missing, "node_backend covers it since vocab.2026-09-07.2"


def test_a_group_with_no_items_is_harmless() -> None:
    assert skill_set_for([{"label": "Empty"}], vocabulary=get_vocabulary()) == ()


def test_a_bracketed_model_name_still_reaches_its_provider() -> None:
    """`Ollama (Llama 3.1)` is not an alias and never will be — the bracket
    carries a model version that changes every few months. The splitter is what
    gets it to `llm_engineering`, which is why resolution belongs here and not
    in a longer and longer alias list."""
    assert resolve_item("Ollama (Llama 3.1)", get_vocabulary()) == {"llm_engineering"}
