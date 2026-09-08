"""Boilerplate reclassification: the phrases, and what they must not touch.

Stage ⑤ extracts "Strong verbal and written communication skills" as a `hard`
requirement at full weight. It resolves to no vocabulary token, so coverage
scores it `missing` — and on the live corpus the top of the gap list, the output
MATCH_SCORING.md §8.2 calls the most valuable thing the system produces, read as
though the operator's largest obstacle was an inability to communicate.

The phrases below were measured against 8,769 unresolved rows with
`extract boilerplate` before they shipped. These tests pin the two rules that
measurement cannot: what the list is forbidden to catch, and that a real
requirement wearing a boilerplate opening survives.
"""

from __future__ import annotations

import pytest

from scout_careers.extract.noise import (
    BOILERPLATE_PHRASES,
    looks_technical,
    matching_phrase,
    phrase_counts,
)


@pytest.mark.parametrize(
    "text",
    [
        "You have strong verbal and written communication skills",
        "Minimum education: Bachelor's degree or an equivalent combination",
        "You excel in fast-paced environments",
        "Your experience demonstrates that you take ownership of your growth",
        "Fluency in English is required",
        "Writes high quality code that is easily understood and used by others",
        "A deep understanding of the regional and cultural nuances is required",
    ],
)
def test_boilerplate_is_recognised(text: str) -> None:
    assert matching_phrase(text, BOILERPLATE_PHRASES) is not None


@pytest.mark.parametrize(
    "text",
    [
        # The operator's explicit decision: a role wanting five years when they
        # have one and a half is a real gap and must keep showing as one. Stage
        # ④ already drops postings above the ceiling, so what survives is within
        # reach — and "within reach" is not "met".
        "You have a total of 1.5+ years of experience as a software engineer.",
        "5+ years of software engineering experience",
        "4-6+ years of professional experience",
        "5 to 7 years of experience at a high-growth technology company",
        # Real requirements that must never be reclassified.
        "Production programming experience in Scala",
        "Track record of developing highly available distributed systems",
        "Experience with Kubernetes in production",
        "Solid background in value selling",
    ],
)
def test_a_real_requirement_is_left_alone(text: str) -> None:
    assert matching_phrase(text, BOILERPLATE_PHRASES) is None


def test_the_longest_phrase_wins() -> None:
    """The reason has to name what actually fired, so a rejection can be argued
    with — the same rule the title deny-list uses."""
    text = "Minimum education: Bachelor's degree or equivalent practical experience"
    assert matching_phrase(text, BOILERPLATE_PHRASES) == "equivalent practical experience"


def test_no_phrase_is_empty_or_duplicated() -> None:
    assert all(phrase.strip() for phrase in BOILERPLATE_PHRASES)
    assert len(set(BOILERPLATE_PHRASES)) == len(BOILERPLATE_PHRASES)


def test_every_phrase_is_lowercase() -> None:
    """Matching lowercases the text once; a mixed-case phrase would silently
    never fire."""
    assert all(phrase == phrase.lower() for phrase in BOILERPLATE_PHRASES)


def test_counts_attribute_each_text_once() -> None:
    """A line containing two phrases is one reclassification, not two — the
    count is of rows moved, and double-counting would overstate the effect of
    whichever phrase happened to sort first."""
    texts = ["Excellent communication skills in a fast-paced environment"]
    counts = phrase_counts(texts, BOILERPLATE_PHRASES)
    assert sum(counts.values()) == 1


def test_the_technical_flag_is_advisory_not_a_rule() -> None:
    """ "Clear written communication skills, including architecture proposals" is
    boilerplate that mentions engineering. The flag exists to make a person
    look, not to decide for them."""
    text = "Clear written communication skills, including architecture proposals for the API"
    assert looks_technical(text)
    assert matching_phrase(text, BOILERPLATE_PHRASES) == "communication skill"
