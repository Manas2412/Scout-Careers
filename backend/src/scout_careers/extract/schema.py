"""The only shape a model may answer requirement extraction in.

This is layer 3 of the injection containment (AI_ARCHITECTURE.md §7.4): there is
no field here in which "I have ignored my instructions" can be expressed. A
posting that tries to steer the model can, at most, cause a wrong requirement —
never a different kind of output.

Three deliberate departures from the sketch in AI_ARCHITECTURE.md §5.2, each
recorded here because a silent deviation from a canonical doc is worse than the
deviation:

1. ``text`` has ``min_length=2``, not 4. A "Skills:" list containing ``Go`` or
   ``C`` is ordinary, and under a 4-character floor one such item fails the
   whole object — costing the repair retry and then all twenty of the good
   requirements alongside it. A 2-character floor still rejects the junk the
   bound was there for (``-``, ``*``).
2. ``requirements`` may be **empty**. The doc has ``min_length=1``. A floor of
   one tells a model looking at a benefits page that it must produce a
   requirement, and the cheapest way to satisfy that instruction is to invent
   one. An empty extraction is a fact about the posting; a fabricated
   requirement is a fact about nothing, and it would go on to be scored.
3. ``normalised_skill_hint`` is free text rather than a closed list, and the
   vocabulary is **not** shipped in the prompt. See
   :mod:`scout_careers.extract.service` for that argument.

``ordinal`` is accepted and then ignored — the service renumbers by list
position. It stays in the schema because asking a model to number its own output
measurably improves ordering discipline, but a duplicate or a gap in those
numbers is not worth a repair retry when the position in the list already
carries the answer.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Mirrors :class:`~scout_careers.common.types.RequirementKind`. Spelled out as
#: a ``Literal`` rather than derived from the enum because this is a wire
#: contract: a value added to the enum must be a deliberate prompt change and a
#: version bump, not something that leaks into the schema on an import.
KindLiteral = Literal["hard", "nice", "responsibility", "tool", "condition"]

#: The seniority vocabulary stage ④'s deny-list already speaks
#: (``FILTER_SENIORITY_DENY``), so a guess here can be compared against the
#: title-derived one rather than being a second, unrelated opinion.
SeniorityGuess = Literal[
    "intern", "junior", "mid", "senior", "staff", "lead", "director", "unknown"
]

#: A posting stating more than this many distinct requirements is either a
#: multi-role advert or a model that has started enumerating sentences. Both are
#: worth refusing rather than storing.
MAX_REQUIREMENTS: Final = 40


class ExtractedRequirement(BaseModel):
    """One line of a job description, as the model reports it.

    Attributes:
        kind: ``hard`` gates the candidate; ``nice`` does not; ``responsibility``,
            ``tool`` and ``condition`` describe the job. Only the first two carry
            coverage weight when scoring (see
            :data:`~scout_careers.common.types.SCORED_REQUIREMENT_KINDS`), so the
            distinction is not cosmetic: a location or shift line filed as
            ``hard`` scores as an unmet gap, and a posting's rank then partly
            measures how many logistical sentences its employer wrote.
        text: The requirement in the posting's own terms, trimmed. Stored
            verbatim so a gap the operator reads can be traced to a line they
            can go and check.
        normalised_skill_hint: Advisory. Code makes the final assignment through
            the controlled vocabulary, and a hint that resolves to nothing is
            discarded rather than minted into a token.
        weight: Emphasis *within* a kind — a requirement stated once in passing
            against one repeated in the summary and the bullet list.
        ordinal: The model's own numbering. Ignored; see the module docstring.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: KindLiteral
    text: Annotated[str, Field(min_length=2, max_length=280)]
    normalised_skill_hint: Annotated[str | None, Field(max_length=80)] = None
    weight: Annotated[float, Field(ge=0.25, le=1.0)] = 1.0
    ordinal: Annotated[int, Field(ge=0)] = 0


class ExtractionResult(BaseModel):
    """Everything one extraction call is permitted to say.

    Attributes:
        requirements: The extracted lines, in the order they should be stored.
            May be empty.
        seniority_guess: What the posting's own text implies, which is not
            always what its title says.
        employment_type_guess: Free text — "full-time", "6-month contract".
            Bounded rather than enumerated because the phrasing is genuinely
            open and nothing downstream branches on it yet.
        notes: One line for the operator when something about the posting is
            odd. Never parsed, never scored, never put in a document.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    requirements: Annotated[list[ExtractedRequirement], Field(max_length=MAX_REQUIREMENTS)] = []
    seniority_guess: SeniorityGuess = "unknown"
    employment_type_guess: Annotated[str | None, Field(max_length=60)] = None
    notes: Annotated[str, Field(max_length=280)] = ""


__all__ = [
    "MAX_REQUIREMENTS",
    "ExtractedRequirement",
    "ExtractionResult",
    "KindLiteral",
    "SeniorityGuess",
]
