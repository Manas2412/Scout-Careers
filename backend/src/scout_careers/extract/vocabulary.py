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
from functools import cache, lru_cache
from pathlib import Path
from typing import ClassVar, Final

import yaml

from scout_careers.common.errors import ConfigError

#: Where the vocabulary lives, relative to this module.
VOCAB_PATH: Final[Path] = Path(__file__).resolve().parent / "vocab" / "skills.yaml"

#: Punctuation that carries no meaning in a skill phrase. ``+`` and ``#`` are
#: deliberately absent — dropping them turns "C++" into "c" and "C#" into "c",
#: which then collide with the C language token and with each other.
_NOISE = re.compile(r"[^\w+#/. ]+")
_SPACES = re.compile(r"\s+")

#: Markers that separate alternatives. Counted only **at or after the first
#: matched alias** — a list is read left to right, so a marker that qualifies it
#: either sits inside it or trails it.
#:
#: Both halves of that window were forced by live rows. Searching the whole
#: sentence accepted "3-6 years in analytics engineering, data science, **or**
#: applied AI with strong SQL and Python" — an "or" governing a different clause
#: entirely, over two tokens the sentence joins with "and". Searching only
#: *between* the first and last alias then rejected the single commonest
#: disjunction in the corpus: the trailing qualifier. "…in C, C++, Rust, Python,
#: **or** comparable low-level environments" puts its marker after the last
#: alternative, as do "or similar", "or equivalent", "or another X".
#:
#: Deliberately absent: the bare comma. "Postgres, Redis, Kafka" is a stack, not
#: a choice, and admitting the comma would score holding one of the three as
#: holding all three.
_INFIX = re.compile(r"\bor\b|/")

#: **Prefix** quantifiers: they scope forward over a list that may never say
#: "or" — "Fluency in at least one programming language (Python, TypeScript,
#: Go)". Counted only when they appear *before* the first matched alias.
_PREFIX = re.compile(r"\bat least one\b|\bone or more\b|\bany of\b|\bone of\b|\beither\b")

#: A count demand wearing a disjunction's clothes. "Working knowledge of **two
#: or more** common cloud ecosystems" contains "or", names one family, and is
#: not satisfied by holding one — the exact over-credit the family gate exists
#: to prevent, arriving through the marker gate instead. Rejected outright
#: rather than scored as a partial: how to weigh "two of these three" is a
#: scoring rule nobody has written, and inventing one here would hide it.
_MULTIPLE_REQUIRED = re.compile(
    r"\b(?:two|three|four|several|multiple|2|3|4)\s+or\s+more\b|\bboth\b|\ball of\b"
)


#: The families whose members are genuine *substitutes* — pick one, ship.
#:
#: Sharing a family is not the same as being interchangeable, and the live
#: corpus made the difference obvious. Every family here is a market where
#: vendors compete for the same slot: you run on AWS **or** GCP, you query
#: Postgres **or** MySQL, you chart in Tableau **or** Power BI.
#:
#: ``ai`` is the family this exists to exclude, and it produced most of the
#: false positives on the first live run. ``llm_engineering``,
#: ``agentic_workflows``, ``prompt_engineering``, ``rag`` and ``llm_evaluation``
#: are *facets of one practice*, not competing choices — "applied LLM systems —
#: RAG, agentic workflows, prompt engineering, evals" wants all four. Prose
#: about AI is also unusually dense with incidental "or"s ("building **or**
#: operating agentic systems", "working on **or** with AI products"), so the
#: family that most needs the conjunction reading is the one most likely to trip
#: a marker test.
#:
#: ``platform`` is excluded for the same reason from the other direction: Git,
#: CI/CD, Docker and Kubernetes are a stack used together, never a menu.
#: ``backend`` is genuinely mixed — Django/Flask/FastAPI compete, while
#: ``microservices`` and ``orm`` do not — and mixed is not good enough here.
ALTERNATION_FAMILIES: Final[frozenset[str]] = frozenset(
    {"cloud", "language", "database", "bi_tool", "frontend"}
)


@cache
def _containment_pattern(alias: str) -> re.Pattern[str]:
    """A whole-word match for one alias, including the ones ``\\b`` cannot see.

    ``\\b`` is a boundary between a word character and a non-word one, so
    ``\\bc\\+\\+\\b`` demands a word character immediately after the second
    ``+`` — meaning it matches "c++x" and never "c++". The same defect silences
    ``c#`` and ``.net``. Those three sat unmatched in every containment scan
    since the pass was written, and being *technology proper nouns* they are
    exactly the aliases it exists to catch.

    So the boundary is chosen per end: ``\\b`` where the alias's own edge
    character is a word character, and a "not adjacent to a word character"
    lookaround where it is not. "abc++" still fails to match ``c++``, which is
    the property ``\\b`` was there for.
    """
    left = r"\b" if alias[:1].isalnum() or alias[:1] == "_" else r"(?<!\w)"
    right = r"\b" if alias[-1:].isalnum() or alias[-1:] == "_" else r"(?!\w)"
    return re.compile(rf"{left}{re.escape(alias)}{right}")


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

    #: Families a containment scan must never match. A *practice* token names a
    #: way of working, and its aliases are ordinary English words — "security",
    #: "performance", "testing", "ownership". Prose mentions ways of working
    #: constantly without demanding them: "Have navigated enterprise production
    #: requirements such as integrations, reliability, observability" matched
    #: three of these at once on the live corpus, and none of the three was a
    #: requirement.
    #:
    #: `ownership` is the sharpest case. The boilerplate pass reclassified "take
    #: ownership of your growth"; a containment scan would resolve whatever
    #: survived to `ownership`, which every resume variant holds — turning
    #: boilerplate into a *met* requirement. Understating is the direction to be
    #: wrong in (MATCH_SCORING.md §3.2), and this would be the other one.
    #:
    #: A technology token is a proper noun. Prose mentions Postgres because the
    #: role uses Postgres.
    UNSAFE_FOR_CONTAINMENT: ClassVar[frozenset[str]] = frozenset({"practice", "analysis"})

    def resolve_within(self, text: str, *, min_alias_length: int = 3) -> tuple[str, ...]:
        """Stage 1b: every token an alias *inside* ``text`` names.

        Args:
            text: A whole requirement sentence, not a phrase.
            min_alias_length: Aliases shorter than this are skipped.

        Returns:
            The tokens found, sorted, longest alias winning where two overlap.

        :meth:`resolve` is an exact whole-phrase lookup, which is right for a
        resume's skills line — those items *are* phrases. A job requirement is a
        sentence, and "Production programming experience in Java" therefore
        resolved to nothing at all while ``java`` sat in the index. On the
        2026-09-07 corpus that shape accounted for a large share of 8,769
        unresolved ``hard`` rows: not a vocabulary gap, a lookup that could not
        see inside the string it was given.

        **Never a practice token.** See :attr:`UNSAFE_FOR_CONTAINMENT`.

        **Whole-word, and never short.** ``go`` appears in "go to market" and
        "ability to go deep"; ``r`` and ``c`` appear in almost every sentence
        written. A containment match on a two-character alias would invent
        coverage, which is the one direction MATCH_SCORING.md §3.2 says never to
        be wrong in. Short aliases are skipped entirely rather than guessed at —
        a role wanting Go states it in a way some longer alias catches, or it
        does not resolve and shows as a gap.
        """
        return tuple(sorted({token for token, _, _ in self._matches(text, min_alias_length)}))

    def _matches(self, text: str, min_alias_length: int) -> tuple[tuple[str, int, int], ...]:
        """Every containment hit as ``(token, start, end)``.

        The offsets exist for :meth:`resolve_alternatives`, which has to know
        whether an "or" sits *between* the alternatives or somewhere else in the
        sentence entirely. :meth:`resolve_within` throws them away.
        """
        lowered = text.lower()
        hits: list[tuple[str, int, int]] = []
        for alias, token in self.alias_index.items():
            if len(alias) < min_alias_length:
                continue
            skill = self.skills.get(token)
            if skill is None or skill.family in self.UNSAFE_FOR_CONTAINMENT:
                continue
            if (match := _containment_pattern(alias).search(lowered)) is not None:
                hits.append((token, match.start(), match.end()))
        return tuple(hits)

    def resolve_one_within(self, text: str, *, min_alias_length: int = 3) -> str | None:
        """The single token ``text`` names, or ``None`` when it names several.

        ``requirement.normalised_skill`` is one column, and a requirement
        reading "Experience with cloud infrastructure (AWS, GCP, or Azure)" is
        an *or*: any one of three satisfies it. Storing one of them is a guess,
        and for some families a costly one — ``aws``/``azure``/``gcp`` share a
        family scored 0.60 for adjacency, so the guess degrades to ``partial``;
        ``java``/``javascript``/``python`` share a family scored 0.00, because
        Python is not a substitute for Java. Storing ``java`` on a line the
        operator satisfies with Python would score ``missing`` and invent a gap.

        So a multi-token line resolves to nothing here. The subset of those lines
        that are genuinely disjunctive is recovered by
        :meth:`resolve_alternatives` into its own column, with its own scoring
        rule; everything this cannot classify stays an honest unmatched
        requirement, which is the same answer it has today.
        """
        found = self.resolve_within(text, min_alias_length=min_alias_length)
        return found[0] if len(found) == 1 else None

    def resolve_alternatives(
        self, text: str, *, min_alias_length: int = 3
    ) -> tuple[str, ...] | None:
        """The tokens of an *any one of these* list, or ``None``.

        Args:
            text: A whole requirement sentence.
            min_alias_length: Passed through to :meth:`resolve_within`.

        Returns:
            Two or more tokens, any one of which satisfies the requirement, or
            ``None`` when the line is not a disjunction this can be sure of.

        A multi-token sentence is one of two things, and they score in opposite
        directions:

        - **"AWS, GCP, or Azure"** — a disjunction. Holding one satisfies it.
          Scored as a conjunction it invents two gaps out of three.
        - **"Python and Django"** — a conjunction. Holding one does *not*
          satisfy it. Scored as a disjunction it invents a match, which is the
          direction MATCH_SCORING.md §3.2 says never to be wrong in.

        Three gates, all required, all structural rather than semantic. Each one
        was added because the live corpus produced a row the previous set got
        wrong; none of them is a guess about what a sentence means.

        1. **One family, and a substitutable one.** Sharing a family is not the
           same as being interchangeable — see :data:`ALTERNATION_FAMILIES`,
           which is an allow-list precisely because "same family" alone accepted
           a pile of ``ai`` rows that were enumerations, not choices. This also
           disposes of "Python and Django", "AWS and Kubernetes".
        2. **A disjunction marker in the right place.** An "or" or a slash must
           sit at or after the first alternative (:data:`_INFIX`); a quantifier
           like "at least one" must sit before it (:data:`_PREFIX`). Position is
           the whole point: an unanchored "or" matched clauses that had nothing
           to do with the token list.
        3. **Not a count demand.** "Two or more cloud ecosystems" satisfies gates
           1 and 2 and is not satisfied by holding one. See
           :data:`_MULTIPLE_REQUIRED`.

        Gate 2 is what makes the ``language`` family safe to admit. Its
        adjacency is 0.00 *deliberately* — Python does not substitute for Java —
        so a language list is precisely the case a single column scored wrong in
        both directions, and precisely the case a marked disjunction gets right.

        This is a pile of heuristics over a sentence, and it should not be the
        permanent answer. The durable one is to have extraction emit the
        alternatives itself, where the model can see the clause structure this
        can only approximate. Until a golden set exists to measure that prompt
        change (task 21), these gates are what can be verified against real rows
        instead of asserted.
        """
        hits = self._matches(text, min_alias_length)
        tokens = tuple(sorted({token for token, _, _ in hits}))
        if len(tokens) < 2:
            return None

        families = {self.skills[token].family for token in tokens if token in self.skills}
        if len(families) != 1 or not families <= ALTERNATION_FAMILIES:
            return None

        lowered = text.lower()
        if _MULTIPLE_REQUIRED.search(lowered):
            return None

        first = min(start for _, start, _ in hits)
        if _INFIX.search(lowered, first) or _PREFIX.search(lowered, 0, first):
            return tokens
        return None

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
    "ALTERNATION_FAMILIES",
    "VOCAB_PATH",
    "Skill",
    "Vocabulary",
    "canonical_key",
    "get_vocabulary",
    "load_vocabulary",
]
