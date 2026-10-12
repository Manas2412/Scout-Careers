"""``scout-careers eval`` — the golden set and the Tier 1 gates.

``status`` reports what the set holds. ``pick`` draws candidate postings from
the live corpus, one slice at a time, and writes a stub case the operator fills
in. ``run`` is the measurement itself.

**``pick`` writes no labels.** It writes the JD text, the source and an empty
label block. The temptation is obvious — the extractor has already produced
requirements for every one of these postings, and pre-filling them would turn
forty hours of labelling into an afternoon of agreeing. That is exactly the
failure AI_ARCHITECTURE.md §10.1 names: "a golden set that is quietly adjusted
to match current behaviour measures nothing". A set labelled from the scorer's
own output cannot ever fail the scorer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from sqlalchemy import func, select
from sqlalchemy import or_ as sa_or

from scout_careers.cli._async import run as run_async
from scout_careers.cli.output import echo, echo_json, echo_table, error
from scout_careers.common.config import get_settings
from scout_careers.common.logging import configure_logging
from scout_careers.db.models import Company, JobPosting
from scout_careers.db.session import session_scope
from scout_careers.eval.cases import (
    GOLDEN_TOTAL,
    SLICE_TARGETS,
    Slice,
    completeness,
    load_cases,
    slice_counts,
)

app = typer.Typer(no_args_is_help=True, help="The scoring golden set and its gates.")

#: ``backend/tests/eval/golden/`` in a source checkout (TEST_PLAN.md §6.1).
DEFAULT_GOLDEN_DIR = Path(__file__).resolve().parents[3] / "tests" / "eval" / "golden"

#: Rough word counts that make a posting a candidate for the length-based
#: slices. Both are deliberately generous: the point is to offer the operator a
#: shortlist to choose from, not to classify for them.
TERSE_MAX_WORDS = 120
VERBOSE_MIN_WORDS = 900


@app.command("status")
def status(
    directory: Annotated[
        Path, typer.Option("--dir", help="Where the cases live.")
    ] = DEFAULT_GOLDEN_DIR,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """What the golden set holds, and what it is still short of."""
    cases = load_cases(directory)
    counts = slice_counts(cases)
    short = list(completeness(cases))

    if as_json:
        echo_json(
            {
                "total": len(cases),
                "target": GOLDEN_TOTAL,
                "by_slice": {name.value: counts[name] for name in SLICE_TARGETS},
                "short": short,
            }
        )
        return

    echo_table(
        ["slice", "have", "target"],
        [[name.value, str(counts[name]), str(target)] for name, target in SLICE_TARGETS.items()],
    )
    echo()
    echo(f"{len(cases)} of {GOLDEN_TOTAL} cases labelled.")
    if short:
        echo()
        echo("  Still short:")
        for line in short:
            echo(f"    {line}")
        echo()
        echo("  `eval run` reports on what exists; the Tier 1 gate is only")
        echo("  meaningful once a slice is complete.")


def _word_count() -> Any:
    """Rough word count of the stored description, in SQL."""
    return func.array_length(func.string_to_array(func.trim(JobPosting.description_text), " "), 1)


#: Title words that make a posting worth *offering* for a slice. Not a label —
#: the operator still reads the JD and decides, and rejecting a suggestion is
#: expected. Without these the first run returned six Supabase platform-
#: engineering roles for the `consulting` slice, because "newest first" over a
#: 7,200-posting corpus is one employer's latest batch.
#:
#: `straddle` has none on purpose: "could two variants plausibly serve this?"
#: is the judgement the slice exists to capture, and a keyword that answered it
#: would be choosing the cases that prove whatever the keyword encodes.
SLICE_TITLE_HINTS: dict[Slice, tuple[str, ...]] = {
    Slice.AI_PRODUCT: (
        "ai",
        "ml",
        "machine learning",
        "applied scien",
        "llm",
        "nlp",
        "research engineer",
    ),
    Slice.BACKEND: (
        "backend",
        "back-end",
        "platform engineer",
        "distributed",
        "infrastructure",
        "api",
    ),
    Slice.CONSULTING: (
        "analyst",
        "consultant",
        "consulting",
        "strategy",
        "strategic",
        "finance",
        "financial",
        "business",
        "operations",
        "program manager",
        "solutions",
    ),
    Slice.MISMATCH: (
        "firmware",
        "embedded",
        "hardware",
        "mechanical",
        "electrical",
        "sales",
        "account executive",
        "recruiter",
        "marketing",
        "designer",
        "nurse",
        "driver",
    ),
}


def _candidates(slice_name: Slice, limit: int) -> Any:
    """Postings worth offering for this slice.

    Only open, unfiltered postings that still carry their description. Length
    slices select on length, the others on title words (:data:`SLICE_TITLE_HINTS`).

    **One posting per company.** The corpus is 7,200 rows across ~150 employers
    and arrives in per-company batches, so any ordering without this returns six
    roles from whoever was crawled last — which is what the first run did.
    """
    word_count = _word_count()
    statement = (
        select(
            JobPosting.id,
            JobPosting.title,
            Company.name,
            word_count,
            JobPosting.description_text,
        )
        .join(Company, Company.id == JobPosting.company_id)
        .where(
            JobPosting.closed_at.is_(None),
            JobPosting.filtered_out.is_(False),
            func.length(JobPosting.description_text) > 200,
        )
        # One per company, newest first within each.
        .distinct(Company.id)
        .order_by(Company.id, JobPosting.first_seen_at.desc())
    )

    if slice_name is Slice.TERSE:
        statement = statement.where(word_count <= TERSE_MAX_WORDS)
    elif slice_name is Slice.VERBOSE:
        statement = statement.where(word_count >= VERBOSE_MIN_WORDS)

    hints = SLICE_TITLE_HINTS.get(slice_name, ())
    if hints:
        statement = statement.where(
            sa_or(*(func.lower(JobPosting.title).like(f"%{hint}%") for hint in hints))
        )
    return statement.limit(limit)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One posting offered for a slice, with everything the writer needs."""

    case_id: str
    title: str
    company: str
    posting_id: str
    words: int
    description: str


def _stub(case_id: str, slice_name: Slice, company: str, posting_id: str) -> dict[str, Any]:
    """An unlabelled case. Every judgement is left blank on purpose."""
    labels: dict[str, Any] = {
        "requirements": [],
        "expected_recommended_variant": None,
        "expected_top_2": [],
        "expected_gaps_include": [],
        "expected_generation": "blocked" if slice_name is Slice.MISMATCH else "allowed",
        "expected_coverage": {},
        "expected_variant_order": [],
    }
    return {
        "id": case_id,
        "slice": slice_name.value,
        "source": {"company": company, "captured_at": "", "posting_id": posting_id},
        "jd_text_file": f"{case_id}.txt",
        "labels": labels,
        "labelled_by": "operator",
        "labelled_at": None,
    }


async def _pick(slice_name: Slice, count: int, directory: Path) -> list[Candidate]:
    existing = load_cases(directory)
    have = slice_counts(existing)[slice_name]
    want = min(count, max(0, SLICE_TARGETS[slice_name] - have))
    if want == 0:
        echo(f"  {slice_name.value} already has {have}/{SLICE_TARGETS[slice_name]} cases.")
        return []

    taken = {case.source.posting_id for case in existing if case.source.posting_id}
    chosen: list[Candidate] = []
    next_index = len(existing) + 1

    async with session_scope() as session:
        # Over-fetch: some candidates are already in the set.
        result = await session.execute(_candidates(slice_name, want * 6))
        for posting_id, title, company, words, description in result:
            if len(chosen) >= want or posting_id in taken:
                continue
            chosen.append(
                Candidate(
                    case_id=f"{next_index:03d}-{slice_name.value}",
                    title=str(title),
                    company=str(company),
                    posting_id=str(posting_id),
                    words=int(words or 0),
                    description=str(description),
                )
            )
            next_index += 1
    return chosen


@app.command("pick")
def pick(
    slice_name: Annotated[Slice, typer.Option("--slice", help="Which slice to fill.")],
    count: Annotated[int, typer.Option("--count", help="How many to offer.")] = 6,
    directory: Annotated[Path, typer.Option("--dir")] = DEFAULT_GOLDEN_DIR,
    write: Annotated[bool, typer.Option("--write", help="Write the stubs.")] = False,
) -> None:
    """Offer postings from the corpus as candidates for one slice.

    Writes the JD text and an empty label block. It does not pre-fill a single
    label, including the ones the extractor has already produced for these
    postings — see this module's docstring.

    The query runs on the event loop; the files are written here, after it has
    finished. Blocking I/O inside the coroutine would stall every other task
    sharing the loop and hold a database connection open for the duration of
    the writes.
    """
    settings = get_settings()
    configure_logging(settings)
    chosen = run_async(_pick(slice_name, count, directory))

    if write and chosen:
        directory.mkdir(parents=True, exist_ok=True)
        for candidate in chosen:
            (directory / f"{candidate.case_id}.txt").write_text(
                candidate.description, encoding="utf-8"
            )
            (directory / f"{candidate.case_id}.yaml").write_text(
                yaml.safe_dump(
                    _stub(candidate.case_id, slice_name, candidate.company, candidate.posting_id),
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

    echo_table(
        ["case", "title", "company", "words"],
        [[c.case_id, c.title[:48], c.company[:22], str(c.words)] for c in chosen],
    )
    echo()
    if write:
        echo(
            f"  Wrote {len(chosen)} stub(s) to {directory}. "
            "Every label is blank; fill them in by hand."
        )
        echo("  Do not consult `score show` while labelling — a set labelled from the")
        echo("  scorer's own output can never fail the scorer (AI_ARCHITECTURE.md §10.1).")
    else:
        echo(f"  {len(chosen)} candidate(s). Nothing written; re-run with --write.")


@app.command("run")
def run_eval(
    directory: Annotated[Path, typer.Option("--dir")] = DEFAULT_GOLDEN_DIR,
    baseline: Annotated[
        Path | None, typer.Option("--baseline", help="A previous report to compare against.")
    ] = None,
) -> None:
    """Score the golden set and check the Tier 1 gates. MATCH_SCORING.md §12.1."""
    cases = load_cases(directory)
    if not cases:
        error(f"No golden cases in {directory}. Run `eval pick --slice <name> --write` first.")
        raise typer.Exit(code=1)
    _ = baseline
    error("The runner is not built yet; `eval status` and `eval pick` are.")
    raise typer.Exit(code=2)


__all__ = ["app"]
