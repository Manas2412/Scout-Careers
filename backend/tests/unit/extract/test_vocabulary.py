"""The controlled vocabulary, and the refusals that keep it controlled.

Coverage scoring is a set intersection between ``requirement.normalised_skill``
and ``resume_variant.skill_set``. Every property tested here is about that
intersection meaning something: that a phrase maps to one token and not two,
that a token cannot be minted by a model, and that a phrase which does not
resolve says so instead of guessing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scout_careers.common.errors import ConfigError
from scout_careers.extract.vocabulary import (
    canonical_key,
    get_vocabulary,
    load_vocabulary,
)

# --------------------------------------------------------------------------
# canonical_key
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("Python", "python"),
        ("  PostgreSQL  ", "postgresql"),
        ("Node.js", "node.js"),
        ("CI/CD", "ci/cd"),
        ("machine   learning", "machine learning"),
        ("Kubernetes (k8s)", "kubernetes k8s"),
        ("FP&A", "fp a"),
    ],
)
def test_canonicalisation_collapses_noise(phrase: str, expected: str) -> None:
    assert canonical_key(phrase) == expected


def test_plus_and_hash_survive_canonicalisation() -> None:
    """The reason `+` and `#` are excluded from the noise class.

    Stripping them turns "C++" into "c" and "C#" into "c", which then collide
    with each other and with the C language token. Three distinct skills would
    silently become one, and a C# developer would score as a C developer.
    """
    assert canonical_key("C++") == "c++"
    assert canonical_key("C#") == "c#"
    assert canonical_key("C") == "c"
    assert len({canonical_key(p) for p in ("C++", "C#", "C")}) == 3


def test_word_order_is_preserved() -> None:
    """MATCH_SCORING.md §3.2 suggests sorting the tokens; we deliberately do not.

    Sorting would make these two the same key. The gain is tolerance of a word
    order nobody writes; the cost is that a lookup stops being explainable.
    """
    assert canonical_key("machine learning") != canonical_key("learning machine")


def test_unicode_is_normalised_before_lookup() -> None:
    """A full-width or ligatured phrase must not be a different skill."""
    assert canonical_key("Ｐｙｔｈｏｎ") == "python"


# --------------------------------------------------------------------------
# The shipped file
# --------------------------------------------------------------------------


def test_the_shipped_vocabulary_loads() -> None:
    vocab = get_vocabulary()
    assert vocab.version.startswith("vocab.")
    assert len(vocab.tokens) > 50
    assert len(vocab.alias_index) > len(vocab.tokens)


@pytest.mark.parametrize(
    ("phrase", "token"),
    [
        ("Python", "python"),
        ("python3", "python"),
        ("C/C++", "cpp"),
        ("Strong C/C++ skills", "cpp"),
        ("Golang", "go"),
        ("k8s", "kubernetes"),
        ("PostgreSQL", "postgres"),
        ("Node.js", "javascript"),
        ("FreeRTOS", "rtos"),
        ("device drivers", "device_drivers"),
        ("FP&A", "fpna"),
        ("Power BI", "power_bi"),
        ("retrieval augmented generation", "rag"),
        # vocab.2026-09-07.2 — added from the intersection of what the six
        # resume variants claim and what an ordinary backend posting asks for.
        ("SQLAlchemy 2.0 (async)", "orm"),
        ("Alembic", "orm"),
        ("Prisma", "orm"),
        ("Pydantic v2", "pydantic"),
        ("Express", "node_backend"),
        ("Fastify", "node_backend"),
        ("WebSockets", "realtime_transport"),
        ("SSE", "realtime_transport"),
        ("Nginx", "nginx"),
        ("Cohere Rerank", "rag"),
        ("Ollama", "llm_engineering"),
    ],
)
def test_real_phrases_resolve(phrase: str, token: str) -> None:
    assert get_vocabulary().resolve(phrase) == token


def test_a_token_resolves_to_itself_without_being_its_own_alias() -> None:
    """Every token and label is indexed, so neither has to be repeated.

    Forgetting to repeat one is exactly how a token ends up in the vocabulary
    with nothing that ever maps to it.
    """
    vocab = get_vocabulary()
    for token in vocab.tokens:
        assert vocab.resolve(token) == token
        assert vocab.resolve(vocab.skills[token].label) == token


def test_an_unknown_phrase_resolves_to_none() -> None:
    """`None` is a real answer, not a failure to try harder.

    It becomes a requirement with no normalised skill, which scoring counts as
    unmatched — understating coverage. Overstating it would tell the operator
    they are a fit when they are not.
    """
    assert get_vocabulary().resolve("competitive ballroom dancing") is None


def test_every_composite_member_exists_and_is_atomic() -> None:
    """A composite pointing at a missing member scores partial coverage against
    a token nobody can ever hold. A composite of composites would make partial
    coverage recursive, which the scorer does not implement."""
    vocab = get_vocabulary()
    for skill in vocab.skills.values():
        for member in skill.composite_of:
            assert member in vocab.skills
            assert not vocab.skills[member].is_composite


# --------------------------------------------------------------------------
# The hint
# --------------------------------------------------------------------------


def test_the_phrase_wins_over_the_hint() -> None:
    """A deterministic hit must never be overridden by a model's opinion, or
    two runs over the same posting stop being comparable."""
    vocab = get_vocabulary()
    assert vocab.resolve_with_hint("Python", "java") == "python"


def test_the_hint_is_consulted_only_when_the_lookup_finds_nothing() -> None:
    vocab = get_vocabulary()
    phrase = "Deep hands-on expertise with modern C++ in production"
    assert vocab.resolve(phrase) is None
    assert vocab.resolve_with_hint(phrase, "c++") == "cpp"


def test_the_hint_cannot_mint_a_token() -> None:
    """The property that makes the vocabulary *controlled*.

    A hint is resolved through the same index as anything else, so a model
    answering with a label of its own invention gets `None` rather than a new
    token that no resume variant can ever match.
    """
    vocab = get_vocabulary()
    assert vocab.resolve_with_hint("something unmappable", "c-plus-plus-programming") is None


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_vocabulary(tmp_path / "nope.yaml")


def test_a_vocabulary_without_a_version_is_refused(tmp_path: Path) -> None:
    """The version is concatenated into `requirement.prompt_version`. Without
    it, editing an alias silently re-ranks every posting already scored."""
    path = write(tmp_path / "v.yaml", "skills:\n  - token: python\n    label: Python\n")
    with pytest.raises(ConfigError, match="version"):
        load_vocabulary(path)


def test_a_duplicate_token_is_refused(tmp_path: Path) -> None:
    path = write(
        tmp_path / "v.yaml",
        "version: t\nskills:\n"
        "  - token: python\n    label: Python\n"
        "  - token: python\n    label: Python 3\n",
    )
    with pytest.raises(ConfigError, match="duplicate token"):
        load_vocabulary(path)


def test_an_alias_claimed_by_two_tokens_is_refused(tmp_path: Path) -> None:
    """One of the two would win by file order, and nothing would say which.

    Every posting mentioning that phrase would then score against a token
    chosen by the order of lines in a YAML file.
    """
    path = write(
        tmp_path / "v.yaml",
        "version: t\nskills:\n"
        '  - token: go\n    label: Go\n    aliases: ["golang"]\n'
        '  - token: golang_other\n    label: Other\n    aliases: ["Golang"]\n',
    )
    with pytest.raises(ConfigError, match="maps to both"):
        load_vocabulary(path)


def test_a_composite_naming_a_missing_member_is_refused(tmp_path: Path) -> None:
    path = write(
        tmp_path / "v.yaml",
        "version: t\nskills:\n  - token: fpna\n    label: FP&A\n    composite_of: [budgeting]\n",
    )
    with pytest.raises(ConfigError, match="do not exist"):
        load_vocabulary(path)


def test_malformed_yaml_is_refused_without_quoting_the_file(tmp_path: Path) -> None:
    path = write(tmp_path / "v.yaml", "version: t\nskills: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_vocabulary(path)


# --------------------------------------------------------------------------
# Containment: finding a token inside a sentence, and what it must not find
# --------------------------------------------------------------------------


def test_a_token_inside_a_sentence_is_found() -> None:
    """`resolve` is an exact whole-phrase lookup, which is right for a resume's
    skills line and wrong for a job requirement. "Production programming
    experience in Java" resolved to nothing while `java` sat in the index."""
    vocab = get_vocabulary()
    assert vocab.resolve("Production programming experience in Java") is None
    assert "java" in vocab.resolve_within("Production programming experience in Java")


def test_several_technologies_in_one_sentence_all_resolve() -> None:
    vocab = get_vocabulary()
    found = vocab.resolve_within("Experience with cloud infrastructure (AWS, GCP, or Azure)")
    assert {"aws", "gcp", "azure"} <= set(found)


def test_a_practice_family_token_is_never_found_by_containment() -> None:
    """The error this guard exists to stop, arrived at from the other side.

    `security_practice`, `performance_tuning`, `ownership` and `testing` have
    ordinary English words for aliases, and prose mentions ways of working
    without demanding them — one live requirement matched three at once. Worse,
    `ownership` is a token every resume variant holds, so a containment match
    would turn boilerplate the reclassifier had just demoted into a *met*
    requirement.
    """
    vocab = get_vocabulary()
    text = (
        "Have navigated enterprise production requirements such as integrations, "
        "reliability, observability, security and performance"
    )
    found = set(vocab.resolve_within(text))
    assert "security_practice" not in found
    assert "performance_tuning" not in found
    assert "ownership" not in found
    assert "testing" not in found


def test_ownership_prose_resolves_to_nothing() -> None:
    text = "A proven track record of taking ownership of complex, ambiguous projects"
    assert get_vocabulary().resolve_within(text) == ()


def test_a_short_alias_is_skipped() -> None:
    """`go` appears in "go to market" and "ability to go deep"; `r` and `c` in
    almost every sentence written. A containment match on those would invent
    coverage, so a role wanting Go either states it in a way a longer alias
    catches or it stays an honest gap."""
    vocab = get_vocabulary()
    assert vocab.resolve_within("We go to market with a partner-led motion") == ()


def test_containment_never_invents_a_token() -> None:
    """Whatever it returns must already be in the vocabulary — the same
    guarantee `resolve` gives. A containment scan that could mint a token would
    be worse than no scan."""
    vocab = get_vocabulary()
    text = "Experience with Kubernetes, Terraform and Postgres in production"
    assert all(token in vocab.skills for token in vocab.resolve_within(text))


def test_a_single_token_sentence_resolves() -> None:
    vocab = get_vocabulary()
    assert vocab.resolve_one_within("Production programming experience in Java") == "java"


def test_an_or_list_resolves_to_nothing() -> None:
    """`requirement.normalised_skill` is one column and this line is an *or*.

    For the cloud family a guess degrades to `partial` through adjacency. For
    the language family, scored 0.00 because Python does not substitute for
    Java, storing `java` on a line the operator satisfies with Python would
    score `missing` — inventing a gap. A line naming several stays unmatched,
    which is exactly what it is today.
    """
    vocab = get_vocabulary()
    text = "Hands-on coding expertise in one or more modern programming languages (Java, JavaScript, Python)"
    assert len(vocab.resolve_within(text)) > 1
    assert vocab.resolve_one_within(text) is None
