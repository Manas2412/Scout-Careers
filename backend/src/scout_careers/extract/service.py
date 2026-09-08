"""Stage ⑤: a job description becomes ``requirement`` rows.

Three separable pieces, kept separable on purpose:

- :func:`prepare` is pure. It turns a model's answer into rows — deduplicating,
  renumbering, resolving skills through the controlled vocabulary — and it needs
  no model, no database and no network to test. Every judgement this stage makes
  that is *ours* rather than the model's lives here.
- :func:`extract_posting` makes the call.
- :func:`store_requirements` writes, replacing the posting's previous set.

**The vocabulary is not shipped in the prompt.** AI_ARCHITECTURE.md §5.2 lists
the closed skill list among the trusted inputs. It is not sent, for two reasons.
The weaker one is cost: ~400 tokens on every extraction, against a hint that
:mod:`~scout_careers.extract.vocabulary` resolves through a 342-entry alias index
anyway — a model answering ``"c++"`` lands on ``cpp`` with or without the list in
front of it. The stronger one is that a closed list in the prompt is an
instruction to *choose from it*, and the cheapest way to obey is to map an
unlisted skill onto the nearest listed one. That converts an honest ``None`` —
which scoring counts as unmatched, understating coverage — into a confident
wrong token, which overstates it. Overstating tells the operator they are a fit
when they are not, and it is the one direction this system must not be wrong in.

If measurement later shows hints missing often, the list goes into the **system**
block, where prompt caching pays for it once per burst rather than once per
posting — not into the user message.

Re-extraction replaces rather than accumulates. ``requirement`` has no
``updated_at`` (DATA_MODEL.md §4.2) because a requirement is not edited: a pass
under a new prompt or vocabulary version supersedes the previous set entirely,
and :func:`store_requirements` deletes before it inserts, in one transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_careers.common.logging import get_logger
from scout_careers.common.types import RequirementKind
from scout_careers.db.models import JobPosting, Requirement
from scout_careers.extract.schema import ExtractionResult
from scout_careers.extract.vocabulary import Vocabulary, canonical_key, get_vocabulary
from scout_careers.llm.base import LLMClient
from scout_careers.llm.cost import RunCost
from scout_careers.llm.enforce import call_structured
from scout_careers.llm.guard import looks_suspicious
from scout_careers.llm.registry import Prompt

log = get_logger(__name__)

FAMILY = "requirement_extraction"

#: ``requirement.weight`` is ``NUMERIC(3, 2)``. Rounding here rather than at the
#: driver keeps the stored value and the in-memory one the same number.
_WEIGHT_QUANTUM = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class PreparedRequirement:
    """One row, ready to insert.

    Attributes:
        kind: The enum, not the wire literal.
        text: The posting's own words.
        normalised_skill: A vocabulary token, or ``None`` when the phrase does
            not resolve. ``None`` is a real answer — see
            :meth:`Vocabulary.resolve`.
        normalised_skill_hint: What the model suggested, stored verbatim. Kept
            because it is what makes :func:`reresolve` possible: a vocabulary
            change can then recompute every token with no model call, and the
            hint carries a large share of the hits ("Proficient in SQL (ideally
            PostgreSQL)" is an alias of nothing and reaches ``sql`` only through
            it).
        weight: Quantised to two decimal places.
        ordinal: Position in the posting, assigned here rather than by the model.
    """

    kind: RequirementKind
    text: str
    normalised_skill: str | None
    normalised_skill_hint: str | None
    weight: Decimal
    ordinal: int


@dataclass(frozen=True, slots=True)
class Extraction:
    """One posting's extraction, with the provenance every row will carry.

    Attributes:
        posting_id: The posting this describes.
        requirements: The prepared rows, in posting order. May be empty.
        seniority_guess: From the text, for comparison against the title-derived
            guess rather than to replace it.
        employment_type_guess: Free text, or ``None``.
        notes: The model's one line for the operator. Never scored.
        model: The resolved provider model ID. Invariant 7.
        prompt_version: ``family@version+vocab.version``. Both halves, because a
            vocabulary change re-maps phrases and therefore re-ranks postings
            exactly as a prompt change does.
        suspicious: The description matched a known injection pattern. An
            observability flag; it gates nothing (see
            :func:`~scout_careers.llm.guard.looks_suspicious`).
        attempts: 1 unless a repair retry fired.
        resolved: How many requirements got a vocabulary token. The stage's own
            health metric — a sharp drop means the vocabulary has drifted away
            from what the corpus says.
    """

    posting_id: str
    requirements: tuple[PreparedRequirement, ...]
    seniority_guess: str
    employment_type_guess: str | None
    notes: str
    model: str
    prompt_version: str
    suspicious: bool = False
    attempts: int = 1

    @property
    def resolved(self) -> int:
        """Count of requirements carrying a vocabulary token."""
        return sum(1 for item in self.requirements if item.normalised_skill is not None)


def version_string(prompt: Prompt, vocabulary: Vocabulary) -> str:
    """Return the provenance string stored on every row this pass produces.

    Args:
        prompt: The prompt used.
        vocabulary: The vocabulary used.

    Returns:
        ``family@version+vocab.version``.

    Both halves are needed for the string to answer the question it exists to
    answer. A row citing only the prompt cannot be told apart from one extracted
    before an alias was added — and after that edit the same JD yields different
    ``normalised_skill`` values, which is a different result, not the same one.
    """
    return f"{prompt.id}+{vocabulary.version}"


def rebind_vocabulary(prompt_version: str, vocabulary: Vocabulary) -> str:
    """Return ``prompt_version`` with only its vocabulary half replaced.

    Args:
        prompt_version: A stored ``family@version+vocab.version``.
        vocabulary: The vocabulary now in force.

    Returns:
        The same prompt half, the new vocabulary half.

    The half that must not move. Re-resolving recomputes ``normalised_skill``
    from stored text; it does not re-read the posting, so the prompt that
    produced that text is exactly the one that produced it before. Stamping the
    *current* prompt version on those rows would claim a pass that never
    happened — and since re-extraction is skipped for rows already at the
    current version, it would also make them permanently un-extractable.
    """
    prompt_part, separator, _ = prompt_version.rpartition("+")
    head = prompt_part if separator else prompt_version
    return f"{head}+{vocabulary.version}"


def _is_unrecoverable(row: Requirement, recomputed: str | None) -> bool:
    """Whether a row's token cannot be recomputed from what is stored.

    Args:
        row: The stored requirement.
        recomputed: What phrase-plus-hint resolution produces now.

    Returns:
        True when the row holds a token that only a hint could have produced,
        and no hint was stored.

    The inference is exact rather than a heuristic about row age. A token that
    survives phrase-only resolution is reproducible by definition. A token that
    does *not*, on a row whose hint is ``NULL``, can only have come from a hint
    that was thrown away — because when a hint is used it is now stored, and
    when it is legitimately absent the token came from the phrase and
    reproduces.

    The residual ambiguity is a row whose token came from the phrase under an
    alias the vocabulary has since **removed**: that also looks like this. It is
    reported rather than resolved — retaining a token the vocabulary no longer
    recognises would overstate coverage, so the count exists to send the
    operator to a re-extraction rather than to let either error stand silently.
    """
    return (
        recomputed is None
        and row.normalised_skill is not None
        and row.normalised_skill_hint is None
    )


@dataclass(slots=True)
class ReresolveOutcome:
    """What a re-resolution pass did.

    Attributes:
        examined: Rows read.
        gained: Rows that had no token and now have one. The point of the pass.
        lost: Rows that had a token and now have none. Counted separately and
            loudly: an edit that renames a token or drops an alias silently
            un-resolves every row that used it, and the operator has to see that
            before it reaches a score.
        changed: Rows whose token moved from one value to another.
        unrecoverable: Rows left untouched because their token cannot be
            recomputed from what is stored — see :func:`_is_unrecoverable`.
            Only re-extraction fixes these, and leaving their version stamp
            alone is what keeps them eligible for it.
        rewritten: Rows written, including those that only needed the new
            version stamp.
    """

    examined: int = 0
    gained: int = 0
    lost: int = 0
    changed: int = 0
    unrecoverable: int = 0
    rewritten: int = 0

    def as_stats(self) -> dict[str, int]:
        """The rollup for the CLI table."""
        return {
            "examined": self.examined,
            "gained": self.gained,
            "lost": self.lost,
            "changed": self.changed,
            "unrecoverable": self.unrecoverable,
            "rewritten": self.rewritten,
        }


async def reresolve(
    session: AsyncSession,
    *,
    vocabulary: Vocabulary | None = None,
    dry_run: bool = False,
    batch_size: int = 500,
) -> ReresolveOutcome:
    """Recompute every stored ``normalised_skill`` against the vocabulary.

    Args:
        session: An open session. The caller commits.
        vocabulary: Injectable; defaults to the packaged one.
        dry_run: Count what would change and write nothing.
        batch_size: Rows per flush.

    Returns:
        What changed.

    **No model calls.** A ``skills.yaml`` edit changes which token a phrase maps
    to; it does not change the phrase. Re-extracting the corpus to pick that up
    would cost ~₹2,900 and produce the same requirement text it already has —
    which in practice would mean the vocabulary stopped being edited, and the
    vocabulary is the thing that makes coverage a set operation rather than a
    language problem.

    The hint is used exactly as extraction used it, which is why it is stored:
    without it this pass would *lower* coverage on every row that resolved
    through a hint rather than through its own text.
    """
    vocab = vocabulary or get_vocabulary()
    outcome = ReresolveOutcome()
    pending = 0

    rows = await session.stream_scalars(select(Requirement))
    async for row in rows:
        outcome.examined += 1
        before = row.normalised_skill
        after = vocab.resolve_with_hint(row.text_, row.normalised_skill_hint)
        version = rebind_vocabulary(row.prompt_version, vocab)

        if _is_unrecoverable(row, after):
            # This row holds a token that only a hint could have produced, and
            # its hint was never stored. Re-resolving would delete a correct
            # answer for a reason that has nothing to do with the vocabulary —
            # the first live run would have destroyed 221 of them against a
            # byte-identical `skills.yaml`.
            #
            # Left entirely alone, version stamp included, so the row stays
            # eligible for the re-extraction that is the only thing able to
            # recover it.
            outcome.unrecoverable += 1
            continue

        if before == after and row.prompt_version == version:
            continue
        if before != after:
            if before is None:
                outcome.gained += 1
            elif after is None:
                outcome.lost += 1
            else:
                outcome.changed += 1

        outcome.rewritten += 1
        if dry_run:
            continue
        row.normalised_skill = after
        row.prompt_version = version
        pending += 1
        if pending >= batch_size:
            await session.flush()
            pending = 0

    if not dry_run and pending:
        await session.flush()

    log.info(
        "requirements_reresolved",
        vocabulary=vocab.version,
        dry_run=dry_run,
        **outcome.as_stats(),
    )
    return outcome


def prepare(
    result: ExtractionResult,
    *,
    vocabulary: Vocabulary | None = None,
) -> tuple[PreparedRequirement, ...]:
    """Turn a validated model answer into rows.

    Args:
        result: The model's answer, already schema-valid.
        vocabulary: Injectable; defaults to the packaged one.

    Returns:
        The rows, in the order the model listed them, renumbered from zero.

    Duplicates are dropped on the canonicalised text, keeping the first. A
    posting that states "Python" in the summary and again in the bullets is one
    requirement, and storing it twice would double its weight in a score that
    the operator reads as a percentage.

    The model's ``ordinal`` is discarded. Position in the list already carries
    the order, and rejecting a duplicated or skipped number would spend a repair
    retry — a second full-JD call — on something that needs no model to fix.
    """
    vocab = vocabulary or get_vocabulary()
    seen: set[str] = set()
    rows: list[PreparedRequirement] = []

    for item in result.requirements:
        key = canonical_key(item.text)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(
            PreparedRequirement(
                kind=RequirementKind(item.kind),
                text=item.text,
                normalised_skill=vocab.resolve_with_hint(item.text, item.normalised_skill_hint),
                normalised_skill_hint=item.normalised_skill_hint,
                weight=Decimal(str(item.weight)).quantize(_WEIGHT_QUANTUM, rounding=ROUND_HALF_UP),
                ordinal=len(rows),
            )
        )
    return tuple(rows)


async def extract_posting(
    client: LLMClient,
    posting: JobPosting,
    *,
    prompt: Prompt,
    max_jd_tokens: int,
    vocabulary: Vocabulary | None = None,
    cost: RunCost | None = None,
    cache_system: bool = True,
) -> Extraction:
    """Extract one posting's requirements.

    Args:
        client: The provider.
        posting: Must carry ``description_text``; stage ④ has already dropped
            the ones that do not.
        prompt: The versioned extraction prompt.
        max_jd_tokens: Per-posting ceiling on the description, from
            ``EXTRACTION_MAX_JD_TOKENS``. This is the bound on a cost attack.
        vocabulary: Injectable.
        cost: The run's accounting and its breaker.
        cache_system: The system block is identical across every posting in a
            run, so caching it is the whole reason this stage is affordable.

    Returns:
        The prepared extraction. Nothing is written.

    Raises:
        SchemaEnforcementFailed: The model could not produce a valid answer.
            Fatal for this posting, never for the run.
        BudgetExhausted: The daily breaker is open.
    """
    vocab = vocabulary or get_vocabulary()
    description = posting.description_text or ""
    suspicious = looks_suspicious(description)

    response = await call_structured(
        client,
        family=FAMILY,
        prompt=prompt,
        schema=ExtractionResult,
        fields={
            "title": posting.title,
            "company": _company_name(posting),
            "posting_id": posting.id,
        },
        untrusted={"jd": description},
        max_untrusted_tokens=max_jd_tokens,
        cost=cost,
        cache_system=cache_system,
    )

    rows = prepare(response.value, vocabulary=vocab)
    extraction = Extraction(
        posting_id=posting.id,
        requirements=rows,
        seniority_guess=response.value.seniority_guess,
        employment_type_guess=response.value.employment_type_guess,
        notes=response.value.notes,
        model=response.model_id,
        prompt_version=version_string(prompt, vocab),
        suspicious=suspicious,
        attempts=response.attempts,
    )

    log.info(
        "requirement_extracted",
        posting_id=posting.id,
        # Counts and provenance only. Never the requirement text: it is derived
        # from the job description and §11.2 keeps that out of the logs.
        requirements=len(rows),
        resolved=extraction.resolved,
        hard=sum(1 for row in rows if row.kind is RequirementKind.HARD),
        seniority_guess=extraction.seniority_guess,
        model_id=extraction.model,
        prompt_version=extraction.prompt_version,
        attempts=response.attempts,
        suspicious_content_flag=suspicious,
    )
    return extraction


async def store_requirements(session: AsyncSession, extraction: Extraction) -> int:
    """Replace a posting's requirement set.

    Args:
        session: An open session. The caller commits.
        extraction: What to store.

    Returns:
        How many rows were inserted.

    Delete-then-insert, not upsert. There is no natural key to upsert on — a
    re-extraction may split one requirement into two or merge two into one — and
    leaving the old rows behind would leave the posting scored against a mixture
    of two prompt versions, which is the one thing ``prompt_version`` exists to
    make impossible.
    """
    await session.execute(
        delete(Requirement).where(Requirement.posting_id == extraction.posting_id)
    )
    session.add_all(
        [
            Requirement(
                posting_id=extraction.posting_id,
                kind=row.kind,
                text_=row.text,
                normalised_skill=row.normalised_skill,
                normalised_skill_hint=row.normalised_skill_hint,
                weight=row.weight,
                ordinal=row.ordinal,
                model=extraction.model,
                prompt_version=extraction.prompt_version,
            )
            for row in extraction.requirements
        ]
    )
    await session.flush()
    return len(extraction.requirements)


def _company_name(posting: JobPosting) -> str:
    """Return the employer's name for the prompt, or a neutral placeholder.

    The company relationship may not be loaded, and a lazy load inside an async
    session raises rather than quietly fetching. The name is a trusted context
    field, not a fact the extraction depends on, so a placeholder is better than
    making every caller eager-load a relationship for one string.
    """
    company = posting.__dict__.get("company")
    name = getattr(company, "name", None)
    return str(name) if name else "(not stated)"


def summarise(extractions: Sequence[Extraction]) -> dict[str, int]:
    """Aggregate a batch, for the run log.

    Args:
        extractions: What the stage produced.

    Returns:
        Counts the operator can act on: how many postings yielded nothing, and
        how many requirements went unresolved. A rising unresolved count is the
        signal that the vocabulary needs entries, and it is the only feedback
        loop this stage has until the ``skill_proposal`` queue exists.
    """
    requirements = sum(len(item.requirements) for item in extractions)
    resolved = sum(item.resolved for item in extractions)
    return {
        "postings": len(extractions),
        "requirements": requirements,
        "resolved": resolved,
        "unresolved": requirements - resolved,
        "empty_postings": sum(1 for item in extractions if not item.requirements),
        "suspicious_postings": sum(1 for item in extractions if item.suspicious),
    }


__all__ = [
    "FAMILY",
    "Extraction",
    "PreparedRequirement",
    "ReresolveOutcome",
    "extract_posting",
    "prepare",
    "rebind_vocabulary",
    "reresolve",
    "store_requirements",
    "summarise",
    "version_string",
]
