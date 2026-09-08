"""Family adjacency: partial credit for a related-but-not-identical skill.

MATCH_SCORING.md §4.2 wants ``ADJACENCY.best(token, skill_set)`` — a way to say
that "Kubernetes in production" against someone who has deployed to ECS is
neither met nor missing. The doc specifies the call and not the data. This
module is the data, read off the ``family`` field every vocabulary token already
carries, scored one number per family in ``adjacency.yaml``.

Two properties matter more than the numbers:

**It never mints a token.** ``best`` returns a token already in the variant's
``skill_set``. Adjacency changes the *level* a requirement scores at; it can
never make the operator appear to hold a skill they do not.

**A family that cannot be scored honestly scores zero.** Several families are
below ``SKILL_ADJACENCY_MIN`` on purpose — ``platform`` holds both ``kubernetes``
and ``git`` — and the file says why for each. Understating coverage is the right
direction to be wrong in; overstating it tells the operator they are a fit when
they are not, which is the failure that costs an evening.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

from scout_careers.common.errors import ScoutError
from scout_careers.extract.vocabulary import Vocabulary, get_vocabulary

#: ``src/scout_careers/scoring/adjacency.yaml``.
ADJACENCY_PATH: Final[Path] = Path(__file__).resolve().parent / "adjacency.yaml"


class AdjacencyError(ScoutError):
    """The adjacency file is unusable."""


@dataclass(frozen=True, slots=True)
class Neighbour:
    """One adjacency hit: a token the variant holds, and what it is worth."""

    token: str
    score: Decimal


@dataclass(frozen=True, slots=True)
class Adjacency:
    """Family scores, and the lookup over them.

    Attributes:
        version: e.g. ``adjacency.2026-09-07.1``. Joined into
            ``SCORING_PROMPT_VERSION`` so an edit re-scores without re-extracting
            — adjacency changes what a score is, not what a requirement is.
        families: Family name to credit, ``0`` to ``1``.
    """

    version: str
    families: dict[str, Decimal]

    def score_for(self, family: str) -> Decimal:
        """The credit two tokens of ``family`` earn against each other."""
        return self.families.get(family, Decimal("0"))

    def best(
        self, token: str, held: frozenset[str], *, vocabulary: Vocabulary | None = None
    ) -> Neighbour | None:
        """The best adjacency between ``token`` and a variant's ``skill_set``.

        Args:
            token: The requirement's ``normalised_skill``.
            held: The variant's ``skill_set``.
            vocabulary: Injectable; defaults to the packaged one.

        Returns:
            The highest-scoring neighbour, or ``None`` when the token is
            unknown, its family scores zero, or the variant holds nothing else
            in that family.

        Ties break on the token name so a rescore reproduces the same answer —
        the same reason rule 6 in §6.1 falls back to the lowest variant ID.
        """
        vocab = vocabulary or get_vocabulary()
        skill = vocab.skills.get(token)
        if skill is None:
            return None
        score = self.score_for(skill.family)
        if score <= 0:
            return None
        siblings = sorted(
            other
            for other in held
            if other != token
            and (sibling := vocab.skills.get(other)) is not None
            and sibling.family == skill.family
        )
        if not siblings:
            return None
        return Neighbour(token=siblings[0], score=score)


def load_adjacency(path: Path = ADJACENCY_PATH) -> Adjacency:
    """Read and validate the adjacency file.

    Args:
        path: The YAML file.

    Returns:
        The parsed table.

    Raises:
        AdjacencyError: Missing version, a score outside ``0..1``, or a family
            that names nothing in the vocabulary.

    That last check is the one worth having. A family renamed in ``skills.yaml``
    and not here would silently stop producing adjacency — every affected
    requirement would drop from ``partial`` to ``missing``, scores would move,
    and nothing would say why.
    """
    document: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    version = document.get("version")
    if not isinstance(version, str) or not version.strip():
        raise AdjacencyError(f"{path.name} has no version; it is half of the score version")

    raw = document.get("families") or {}
    if not isinstance(raw, dict):
        raise AdjacencyError(f"{path.name}: `families` must be a mapping of family to score")

    families: dict[str, Decimal] = {}
    for family, value in raw.items():
        try:
            score = Decimal(str(value))
        except ArithmeticError as exc:  # pragma: no cover - malformed YAML scalar
            raise AdjacencyError(f"family {family!r} has a non-numeric score {value!r}") from exc
        if not Decimal("0") <= score <= Decimal("1"):
            raise AdjacencyError(f"family {family!r} scores {score}, outside 0..1")
        families[str(family)] = score

    known = {skill.family for skill in get_vocabulary().skills.values()}
    unknown = sorted(set(families) - known)
    if unknown:
        raise AdjacencyError(
            f"{path.name} names families that no vocabulary token has: {unknown}. "
            "A renamed family would silently stop producing adjacency."
        )

    return Adjacency(version=version.strip(), families=families)


@lru_cache(maxsize=1)
def get_adjacency() -> Adjacency:
    """The packaged adjacency table, loaded once."""
    return load_adjacency()


__all__ = [
    "ADJACENCY_PATH",
    "Adjacency",
    "AdjacencyError",
    "Neighbour",
    "get_adjacency",
    "load_adjacency",
]
