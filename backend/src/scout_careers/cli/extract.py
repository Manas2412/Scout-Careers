"""``scout-careers extract`` — run stage ⑤ over the corpus.

Two commands, and the split matters. ``estimate`` costs the work without making
a single model call; ``postings`` does it. A backfill over ~1,500 surviving
postings is the largest single spend this system will make, and spending it
should require having seen the number first.

Failure is per posting. A description the model cannot produce a valid answer
for is one posting logged and skipped, never a run that stops — but the budget
breaker *is* a run-level stop, because a breaker that keeps going is not a
breaker.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Annotated

import typer
from sqlalchemy import Select, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from scout_careers.cli._async import run as run_async
from scout_careers.cli.output import echo, echo_json, echo_table, error
from scout_careers.common.config import Settings, get_settings
from scout_careers.common.logging import configure_logging
from scout_careers.common.types import RequirementKind
from scout_careers.db.models import JobPosting, Requirement
from scout_careers.db.session import session_scope
from scout_careers.extract.noise import looks_technical, matching_phrase, phrase_counts
from scout_careers.extract.service import (
    FAMILY,
    Extraction,
    ReresolveOutcome,
    extract_posting,
    reresolve,
    store_requirements,
    summarise,
    version_string,
)
from scout_careers.extract.vocabulary import get_vocabulary
from scout_careers.llm.base import BudgetExhausted, LLMClient, SchemaEnforcementFailed
from scout_careers.llm.bedrock import BedrockClient
from scout_careers.llm.cost import RunCost, prices_for
from scout_careers.llm.registry import PromptRegistry

app = typer.Typer(no_args_is_help=True, help="Extract requirements from job descriptions.")

#: Per-posting output, for the estimate only. Input is measured from the real
#: descriptions; output cannot be known before the call, so this is the one
#: assumption in the number.
#:
#: 1,200, not the 700 of AI_ARCHITECTURE.md §5.2. Measured over the first ten
#: real extractions: 1,081–1,317 output tokens for 15–19 requirements. The 700
#: was written before anything had been extracted and made the estimate 40% low,
#: which is the wrong direction for a number whose whole job is to be seen
#: before the money is spent.
_ASSUMED_OUTPUT_TOKENS = 1_200

#: Characters per token, matching :mod:`~scout_careers.llm.guard`. The same
#: approximation on purpose — an estimate that used a different one would
#: disagree with the ceiling that actually applies.
_CHARS_PER_TOKEN = 4


def _candidates(
    limit: int | None, prompt_version: str, *, force: bool
) -> Select[tuple[JobPosting]]:
    """Postings stage ④ let through that still need extracting.

    The "already done" test is a ``NOT EXISTS`` rather than a per-posting query.
    That is not only a round-trip saving: it puts the exclusion *before* the
    ``LIMIT``, so ``--limit 20`` means twenty postings that still need work
    rather than twenty candidates of which some number are already finished.

    Ordered newest-first, because a backfill the breaker interrupts should have
    spent the budget on the postings most likely to still be open.
    """
    stmt = (
        select(JobPosting)
        .options(selectinload(JobPosting.company))
        .where(
            JobPosting.filtered_out.is_(False),
            JobPosting.closed_at.is_(None),
            func.length(JobPosting.description_text) > 0,
        )
    )
    if not force:
        stmt = stmt.where(
            ~exists().where(
                Requirement.posting_id == JobPosting.id,
                Requirement.prompt_version == prompt_version,
            )
        )
    stmt = stmt.order_by(JobPosting.first_seen_at.desc())
    return stmt.limit(limit) if limit else stmt


async def _pending(
    session: AsyncSession, limit: int | None, prompt_version: str, force: bool
) -> list[JobPosting]:
    """Return the postings to extract."""
    result = await session.execute(_candidates(limit, prompt_version, force=force))
    return list(result.scalars())


@app.command("estimate")
def estimate(
    limit: Annotated[int | None, typer.Option(help="Cap the postings considered.")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Cost the backfill without calling a model.

    Input tokens are measured from the actual descriptions, capped exactly as
    the guard will cap them. Output is assumed, because it cannot be known —
    which is why the number is called an estimate and printed before anything
    is spent.
    """
    settings = get_settings()
    configure_logging(settings)
    prompt = PromptRegistry().get(FAMILY)
    version = version_string(prompt, get_vocabulary())

    async def work() -> dict[str, object]:
        async with session_scope() as session:
            postings = await _pending(session, limit, version, force=False)
            done = await session.scalar(
                select(func.count())
                .select_from(Requirement)
                .where(Requirement.prompt_version == version)
            )
        ceiling = settings.extraction_max_jd_tokens
        input_tokens = sum(
            min(ceiling, len(posting.description_text) // _CHARS_PER_TOKEN) for posting in postings
        )
        output_tokens = len(postings) * _ASSUMED_OUTPUT_TOKENS
        prices = prices_for(settings, "fast")
        cost = (
            (Decimal(input_tokens) * prices.input_usd + Decimal(output_tokens) * prices.output_usd)
            / Decimal(1_000_000)
            * settings.llm_inr_per_usd
        ).quantize(Decimal("0.01"))
        return {
            "prompt_version": version,
            "postings_pending": len(postings),
            "requirements_already_stored": int(done or 0),
            "input_tokens": input_tokens,
            "assumed_output_tokens": output_tokens,
            "estimated_cost_inr": str(cost),
            "daily_budget_inr": str(settings.llm_daily_budget_inr),
        }

    report = run_async(work())
    if as_json:
        echo_json(report)
        return
    echo_table(["metric", "value"], [[key, str(value)] for key, value in report.items()])
    if Decimal(str(report["estimated_cost_inr"])) > settings.llm_daily_budget_inr:
        echo(
            "\nThe estimate exceeds the daily budget. The breaker will stop the run "
            "part-way; re-run to continue, or raise LLM_DAILY_BUDGET_INR."
        )


@app.command("reresolve")
def reresolve_command(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report and write nothing.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Recompute every stored skill token against the current vocabulary.

    **Issues no model calls.** Editing ``skills.yaml`` changes which token a
    phrase maps to, not the phrase — so the corpus can be re-resolved for
    nothing rather than re-extracted for ~₹2,900. That is what makes the
    unresolved-phrase queue in ``scripts/requirements-audit.sh`` something to act
    on rather than a report nobody can afford to answer.

    Run it after any change to ``skills.yaml``. Read ``lost`` before ``gained``:
    a renamed token or a deleted alias un-resolves every row that used it, and
    that is the failure this command can cause.
    """
    settings = get_settings()
    configure_logging(settings)
    vocabulary = get_vocabulary()

    async def work() -> ReresolveOutcome:
        async with session_scope() as session:
            outcome = await reresolve(session, vocabulary=vocabulary, dry_run=dry_run)
            if not dry_run:
                await session.commit()
            return outcome

    outcome = run_async(work())
    if as_json:
        echo_json({**outcome.as_stats(), "vocabulary": vocabulary.version, "dry_run": dry_run})
        return
    echo_table(
        ["metric", "value"],
        [
            ["vocabulary", vocabulary.version],
            *[[k, f"{v:,}"] for k, v in outcome.as_stats().items()],
        ],
    )
    if outcome.lost:
        echo("")
        echo(
            f"  {outcome.lost:,} requirement(s) LOST their token. An alias was removed or "
            "renamed; those lines now count as unmatched."
        )
    if outcome.unrecoverable:
        echo("")
        echo(
            f"  {outcome.unrecoverable:,} requirement(s) could not be recomputed and were "
            "left untouched. Their token came from a model hint that predates the hint "
            "column, so nothing stored can reproduce it."
        )
        echo("  Re-extract those postings to make them re-resolvable:")
        echo("    scout-careers extract postings --force")
    if dry_run:
        echo("")
        echo("Nothing written.")


@app.command("postings")
def postings(
    limit: Annotated[int | None, typer.Option(help="Extract at most this many.")] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-extract even at the current prompt version.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Extract requirements and store them.

    Each posting is one transaction. A run that stops half way — breaker,
    interrupt, a crash — leaves every posting it finished fully stored and every
    one it did not entirely untouched, so re-running continues rather than
    repairs.
    """
    settings = get_settings()
    configure_logging(settings)
    result = run_async(_extract_all(settings, limit=limit, force=force))
    if as_json:
        echo_json(result)
        return
    echo_table(["metric", "value"], [[key, str(value)] for key, value in result.items()])


async def _extract_all(settings: Settings, *, limit: int | None, force: bool) -> dict[str, object]:
    """Run the stage. Returns the rollup."""
    prompt = PromptRegistry().get(FAMILY)
    vocabulary = get_vocabulary()
    version = version_string(prompt, vocabulary)
    cost = RunCost.from_settings(settings)
    client: LLMClient = BedrockClient(settings)
    gate = asyncio.Semaphore(settings.llm_max_concurrency)

    done: list[Extraction] = []
    failed = 0
    stopped = ""

    async def one(posting: JobPosting) -> Extraction:
        """Extract and store one posting, under the concurrency gate.

        The write happens inside the gate rather than after all the calls, so a
        run that is interrupted has already persisted everything it paid for.
        Each posting is its own transaction for the same reason.
        """
        async with gate:
            extraction = await extract_posting(
                client,
                posting,
                prompt=prompt,
                max_jd_tokens=settings.extraction_max_jd_tokens,
                vocabulary=vocabulary,
                cost=cost,
                cache_system=settings.llm_cache_enabled,
            )
            async with session_scope() as session:
                await store_requirements(session, extraction)
            echo(
                f"  ok {posting.id} — {len(extraction.requirements)} requirement(s), "
                f"{extraction.resolved} resolved"
            )
            return extraction

    try:
        async with session_scope() as session:
            pending = await _pending(session, limit, version, force)

        if not pending:
            echo("Nothing to extract.")
            return {"postings": 0, "requirements": 0}

        echo(
            f"Extracting {len(pending)} posting(s) at {version}, "
            f"{settings.llm_max_concurrency} at a time …"
        )

        # Concurrent, because serial would make a 1,500-posting backfill a
        # two-hour job. The breaker still bounds it: `cost.guard()` runs before
        # every call, so once the budget is spent the queued tasks fail
        # immediately and cheaply rather than needing to be cancelled.
        outcomes = await asyncio.gather(
            *(one(posting) for posting in pending), return_exceptions=True
        )

        for posting, outcome in zip(pending, outcomes, strict=True):
            if isinstance(outcome, Extraction):
                done.append(outcome)
            elif isinstance(outcome, BudgetExhausted):
                # A run-level stop, unlike everything else here. The postings
                # that never ran keep their absence of rows, which is what makes
                # the next run continue rather than repeat.
                stopped = str(outcome)
            elif isinstance(outcome, SchemaEnforcementFailed):
                failed += 1
                error(f"  !! {posting.id} — {outcome}")
            elif isinstance(outcome, BaseException):
                failed += 1
                error(f"  !! {posting.id} — {type(outcome).__name__}: {outcome}")
    finally:
        await client.aclose()

    if stopped:
        error(f"Budget breaker opened: {stopped}")

    rollup: dict[str, object] = {**summarise(done), "failed": failed, **cost.as_stats()}
    if stopped:
        rollup["stopped"] = stopped
    return rollup


__all__ = ["app"]


@app.command("boilerplate")
def boilerplate(
    words: Annotated[
        str | None,
        typer.Option("--words", help="Comma-separated candidate phrases to simulate."),
    ] = None,
    top: Annotated[int, typer.Option("--top", help="Rows to show.")] = 40,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Find, simulate and (later) reclassify boilerplate requirements.

    With no arguments it answers the question that has to come first: what is
    actually in the ``hard`` bucket that resolves to no vocabulary token. Those rows are
    scored ``missing`` at full weight on every posting that carries them, so
    they are simultaneously the reason scores are depressed and the reason the
    gap list reads as though the operator's biggest problem is communication
    skills.

    ``--words`` then simulates a candidate phrase list against that same
    population, exactly as ``filter try-titles`` does for job titles, and flags
    any phrase that would also catch a line naming a technology. Nothing is
    written by either mode.
    """
    settings = get_settings()
    configure_logging(settings)

    async def work() -> list[tuple[int, str]]:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Requirement.text_, func.count())
                    .where(
                        Requirement.kind == RequirementKind.HARD,
                        Requirement.normalised_skill.is_(None),
                    )
                    .group_by(Requirement.text_)
                    .order_by(func.count().desc())
                )
            ).all()
            return [(int(count), str(text)) for text, count in rows]

    counted = run_async(work())
    total = sum(count for count, _ in counted)

    if words:
        candidates = tuple(word.strip().lower() for word in words.split(",") if word.strip())
        texts = [text for count, text in counted for _ in range(count)]
        counts = phrase_counts(texts, candidates)
        flagged = {
            phrase: [
                text
                for _, text in counted
                if matching_phrase(text, candidates) == phrase and looks_technical(text)
            ]
            for phrase in candidates
        }
        if as_json:
            echo_json(
                {
                    "unresolved_hard": total,
                    "candidates": [
                        {"phrase": phrase, "matches": counts[phrase], "technical": flagged[phrase]}
                        for phrase in candidates
                    ],
                }
            )
            return
        echo_table(
            ["candidate", "matches", "technical"],
            [
                [phrase, str(counts[phrase]), str(len(flagged[phrase])) if flagged[phrase] else "-"]
                for phrase in candidates
            ],
        )
        moved = sum(1 for _, text in counted if matching_phrase(text, candidates))
        echo("")
        echo(
            f"{moved:,} of {len(counted):,} distinct unresolved hard requirement(s) "
            f"would be reclassified, covering {total:,} row(s) in total."
        )
        for phrase, hits in flagged.items():
            if not hits:
                continue
            echo("")
            echo(f"  {phrase!r} also matches lines that name a technology:")
            for text in hits[:4]:
                echo(f"    {text[:88]}")
        echo("")
        echo("  Nothing written. These are candidates, not a deny list.")
        return

    if as_json:
        echo_json(
            {
                "unresolved_hard_rows": total,
                "distinct_texts": len(counted),
                "top": [{"count": count, "text": text} for count, text in counted[:top]],
            }
        )
        return

    echo_table(
        ["rows", "requirement text"],
        [[str(count), text[:96]] for count, text in counted[:top]],
    )
    echo("")
    echo(
        f"{total:,} hard requirement row(s) across {len(counted):,} distinct texts resolve "
        "to no vocabulary token. Every one is scored `missing` at full weight."
    )
    echo(
        "  A line here that describes the candidate rather than the work is boilerplate: "
        "simulate it with `extract boilerplate --words '...'` before adding it anywhere."
    )
