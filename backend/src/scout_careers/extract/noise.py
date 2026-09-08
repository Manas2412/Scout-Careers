"""Boilerplate requirements: the lines a job description states about everyone.

Stage ⑤ extracts "Strong verbal and written communication skills", "Bachelor's
degree or equivalent practical experience" and "You excel in fast-paced
environments" as ``hard`` requirements at full weight. They are not demands the
vocabulary can express, they resolve to no token, and coverage therefore scores
them ``missing`` — so on the 2026-09-07 corpus the single biggest thing blocking
the operator's job search was, according to their own gap list, a lack of
communication skills.

That is three separate harms from one cause:

1. **Every score is depressed.** The lines sit in the ``hard`` bucket at weight
   1.00 and can never be met, so they inflate the denominator of ``H`` on
   1,247 postings.
2. **The gap list stops being useful.** MATCH_SCORING.md §8.2 calls it the most
   valuable output; its top rows were "communication skills" and "Bachelor's
   degree" against 31 and 23 postings.
3. **Verbose postings fail outright.** A description with 30 real requirements
   plus 20 boilerplate lines exceeds ``MAX_REQUIREMENTS`` and the whole
   extraction is refused — 46 of 100 postings in one run.

**Reclassified, never deleted.** These become ``condition``: extracted, visible,
carrying no coverage weight. Exactly the treatment "four 10-hour shifts covering
weekends" already gets, and for the same reason — the operator wants to see that
a role wants a degree, it just must not be scored as a skill they lack.

**Deterministic and free.** No model call. The rows are already stored, so this
fixes the whole corpus rather than only what is extracted next, and it costs
nothing to re-run after editing the phrase list. A prompt change would cost a
full re-extraction to fix what is already on disk, and would still be a model
being asked nicely rather than a rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

#: Phrases that mark a requirement as boilerplate. Matched case-insensitively
#: as substrings against the requirement text, because these arrive as whole
#: sentences with employer-specific padding around a stable core: "You have
#: strong verbal and written communication skills that support effective
#: collaboration" and "Strong verbal and written communication skills" are one
#: phrase in two dressings.
#:
#: Every entry is measured against the corpus by `extract boilerplate` before it
#: goes in. The bar is the one `FILTER_KEYWORD_DENY` uses: an entry that also
#: matches a real technical requirement is rejected, however much noise it
#: catches — a filter that removes a genuine "distributed systems" line to be
#: rid of "fast-paced environment" has made the score worse, not better.
BOILERPLATE_PHRASES: Final[tuple[str, ...]] = ()


@dataclass(frozen=True, slots=True)
class NoiseOutcome:
    """What a reclassification pass found."""

    considered: int = 0
    reclassified: int = 0
    by_phrase: dict[str, int] = field(default_factory=dict)
    samples: list[tuple[str, str]] = field(default_factory=list)


def matching_phrase(text: str, phrases: Sequence[str]) -> str | None:
    """The first boilerplate phrase ``text`` contains, or ``None``.

    Most specific first, so the reason names the longest match rather than one
    of its words — the same rule the title deny-list uses, and for the same
    reason: a reason an operator can argue with has to name what actually fired.
    """
    lowered = text.lower()
    for phrase in sorted(phrases, key=lambda entry: (-len(entry), entry)):
        if phrase in lowered:
            return phrase
    return None


def phrase_counts(texts: Iterable[str], phrases: Sequence[str]) -> dict[str, int]:
    """How many of ``texts`` each phrase would reclassify."""
    counts: dict[str, int] = dict.fromkeys(phrases, 0)
    for text in texts:
        phrase = matching_phrase(text, phrases)
        if phrase is not None:
            counts[phrase] += 1
    return counts


#: Words whose presence means a line is about the *work*, not about the person.
#: Used only to flag a candidate phrase that would reclassify such a line — the
#: `Full Stack Engineer - Internal Audit` check, moved to this domain.
_TECHNICAL = re.compile(
    r"\b(python|java|typescript|javascript|go|rust|sql|postgres|kubernetes|docker|"
    r"aws|azure|gcp|terraform|react|api|backend|frontend|distributed|latency|"
    r"throughput|pipeline|microservice|database|infrastructure)\b",
    re.IGNORECASE,
)


def looks_technical(text: str) -> bool:
    """Whether a requirement names a technology or an engineering concern.

    Deliberately advisory. "Strong communication skills, especially when
    explaining distributed systems trade-offs" is boilerplate *and* technical,
    and only a person can say which reading wins.
    """
    return bool(_TECHNICAL.search(text))


__all__ = [
    "BOILERPLATE_PHRASES",
    "NoiseOutcome",
    "looks_technical",
    "matching_phrase",
    "phrase_counts",
]
