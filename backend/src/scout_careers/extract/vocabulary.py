"""The controlled vocabulary, and stage 1 of the resolver.

Coverage scoring is a set operation — ``requirement.normalised_skill`` against
``resume_variant.skill_set`` — and that only works if both sides speak the same
closed language. This module is that language.

Stage 1 is a deterministic exact lookup over a canonicalised alias index, and it
resolves the large majority of phrases. That matters for four reasons, none of
them performance (MATCH_SCORING.md §3.3):

- **Reproducibility.** Comparing two scoring prompt versions over the same
  postings measures the prompt change only if normalisation is identical between
  the runs. A model in this path would make the A/B measure its own noise.
- **Cost.** A job description yields 15–40 requirements. One model call each
  would be over a thousand extra calls a day against a budget of ₹80.
- **Drift.** A model upgrade silently re-maps phrases, which silently re-ranks
  every posting. A YAML change is a diff in a pull request.
- **Auditability.** "Why did *Strong C/C++ skills* become ``cpp``?" has a
  one-line answer that points at a file.

Stages 2 (trigram) and 3 (constrained model fallback) are **not implemented
here**; they need a ``skill_alias`` table and a ``skill_proposal`` queue. An
unresolved phrase currently returns ``None``, which is honest — it becomes a
requirement with no normalised skill, and scoring treats it as unmatched rather
than guessing.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml

from scout_careers.common.errors import ConfigError

#: Where the vocabulary lives, relative to this module.
VOCAB_PATH: Final[Path] = Path(__file__).resolve().parent / "vocab" / "skills.yaml"

#: Punctuation that carries no meaning in a skill phrase. ``+`` and ``#`` are
#: deliberately absent — dropping them turns "C++" into "c" and "C#" into "c",
#: which then collide with the C language token and with each other.
_NOISE = re.compile(r"[^\w+#/. ]+")
_SPACES = re.compile(r"\s+")


def canonical_key(phrase: str) -> str:
    """Reduce a phrase to its lookup key.

    Args:
        phrase: Free text, from a job description or a vocabulary alias.

    Returns:
        A casefolded, punctuation-stripped, whitespace-collapsed key.

    Word order is **preserved**. MATCH_SCORING.md §3.2 suggests sorting the
    tokens, which would make "data engineer" and "engineer data" the same key —
    harmless — but also "machine learning" and "learning machine", and more to
    the point it destroys the ability to tell "real time" from "time real" in
    any alias where order carries meaning. Sorting buys very little and costs
    the one property that makes a lookup explainable.
    """
    text = unicodedata.normalize("NFKC", phrase).casefold()
    text = _NOISE.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


@dataclass(frozen=True, slots=True)
class Skill:
    """One vocabulary entry.

    Attributes:
        token: The canonical identifier stored in the database.
        label: Human-readable name, for the UI and the digest.
        family: Grouping, for reporting rather than for scoring.
        aliases: Surface forms that resolve to this token.
        composite_of: Member tokens, when this competency decomposes. This is
            how partial coverage becomes principled rather than a judgement
            call: holding two of the three members of ``fpna`` is a defensible
            ``partial``, not somebody's opinion.
    """

    token: str
    label: str
    family: str
    aliases: tuple[str, ...] = ()
    composite_of: tuple[str, ...] = ()

    @property
    def is_composite(self) -> bool:
        """Whether this token decomposes into members."""
        return bool(self.composite_of)


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """The loaded vocabulary and its lookup index.

    Attributes:
        version: e.g. ``vocab.2026-09-06``. Concatenated into
            ``requirement.prompt_version`` so a vocabulary change invalidates
            extraction exactly like a prompt change.
        skills: Every entry, by token.
        alias_index: Canonical key to token. Includes each token's own label
            and the token itself, so ``python`` resolves without needing to be
            listed as an alias of itself.
    """

    version: str
    skills: dict[str, Skill] = field(default_factory=dict)
    alias_index: dict[str, str] = field(default_factory=dict)

    @property
    def tokens(self) -> tuple[str, ...]:
        """Every token, sorted. This is the closed list a model may choose from."""
        return tuple(sorted(self.skills))

    def resolve(self, phrase: str) -> str | None:
        """Stage 1: map a phrase to a token, or report that it cannot.

        Args:
            phrase: Free text from a job description.

        Returns:
            The token, or ``None`` when no alias matches.

        ``None`` is a real answer, not a failure to try harder. A phrase that
        does not resolve becomes a requirement with no ``normalised_skill``, and
        scoring counts it as unmatched — which understates coverage. That is the
        right direction to be wrong in: overstating coverage tells the operator
        they are a fit when they are not.
        """
        return self.alias_index.get(canonical_key(phrase))

    def resolve_with_hint(self, phrase: str, hint: str | None = None) -> str | None:
        """Stage 1, then the model's advisory hint.

        Args:
            phrase: The requirement text, e.g. "Strong C/C++ skills".
            hint: ``normalised_skill_hint`` from the extraction response.

        Returns:
            A token, or ``None``.

        The phrase is tried first, so a deterministic hit always wins and the
        result stays reproducible. The hint is consulted only when the lookup
        finds nothing — which is the case an alias list cannot cover, because
        the qualifier is unbounded: "Strong C/C++ skills", "Excellent C++",
        "Deep C++ experience" and "Solid grasp of modern C++" are one skill and
        four phrasings, and enumerating them is a losing game.

        **The hint cannot mint a token.** It is resolved through the same index,
        so a model answering ``"c-plus-plus-programming"`` gets ``None`` rather
        than inventing vocabulary. That is what makes the vocabulary controlled.
        """
        if (token := self.resolve(phrase)) is not None:
            return token
        if hint:
            return self.resolve(hint)
        return None

    def members_of(self, token: str) -> tuple[str, ...]:
        """Return a composite's members, or an empty tuple for an atomic token."""
        skill = self.skills.get(token)
        return skill.composite_of if skill else ()


def load_vocabulary(path: Path | None = None) -> Vocabulary:
    """Read and index the vocabulary file.

    Args:
        path: The YAML file. Defaults to the packaged one.

    Returns:
        The indexed vocabulary.

    Raises:
        ConfigError: On a missing file, a missing version, a duplicate token, a
            duplicate alias, or a composite naming a token that does not exist.

    Every one of those refusals is about a silent failure. A duplicate alias
    means one of two tokens wins by file order and nobody can tell which. A
    composite pointing at a missing member scores as partially covered against
    a member that can never be held.
    """
    source = path or VOCAB_PATH
    if not source.is_file():
        raise ConfigError(f"skill vocabulary not found at {source}")

    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"skill vocabulary is not valid YAML: {exc}") from None

    version = str(document.get("version") or "").strip()
    if not version:
        raise ConfigError(
            "skill vocabulary needs a `version`; it is concatenated into "
            "requirement.prompt_version so a change re-triggers extraction"
        )

    skills: dict[str, Skill] = {}
    alias_index: dict[str, str] = {}

    for entry in document.get("skills") or []:
        token = str(entry.get("token") or "").strip()
        if not token:
            raise ConfigError("every vocabulary entry needs a `token`")
        if token in skills:
            raise ConfigError(f"duplicate token {token!r} in the skill vocabulary")

        skill = Skill(
            token=token,
            label=str(entry.get("label") or token),
            family=str(entry.get("family") or "other"),
            aliases=tuple(str(alias) for alias in entry.get("aliases") or ()),
            composite_of=tuple(str(member) for member in entry.get("composite_of") or ()),
        )
        skills[token] = skill

        # The token and its label resolve to themselves, so neither has to be
        # repeated in the alias list — and forgetting to repeat one is exactly
        # the sort of omission that produces a token nothing ever maps to.
        for surface in (token, skill.label, *skill.aliases):
            key = canonical_key(surface)
            if not key:
                continue
            existing = alias_index.get(key)
            if existing is not None and existing != token:
                raise ConfigError(
                    f"alias {surface!r} maps to both {existing!r} and {token!r}. "
                    "One would win by file order and the choice would be invisible."
                )
            alias_index[key] = token

    for skill in skills.values():
        missing = [member for member in skill.composite_of if member not in skills]
        if missing:
            raise ConfigError(
                f"composite {skill.token!r} names member(s) {missing} that do not exist. "
                "Partial coverage would be scored against a token nobody can hold."
            )

    return Vocabulary(version=version, skills=skills, alias_index=alias_index)


@lru_cache(maxsize=1)
def get_vocabulary() -> Vocabulary:
    """Return the process-wide vocabulary, loaded once."""
    return load_vocabulary()


__all__ = [
    "VOCAB_PATH",
    "Skill",
    "Vocabulary",
    "canonical_key",
    "get_vocabulary",
    "load_vocabulary",
]
