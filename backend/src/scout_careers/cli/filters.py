"""``scout-careers filter`` — apply stage ④ to the corpus, or inspect it.

Discovery runs screen the companies they touch. This is the other case: the
deny lists changed, or a backlog predates the filter being wired in at all, and
the whole corpus has to be re-judged.

Kept out of the nightly run deliberately. A full recompute after an edited
``FILTER_LOCATION_ALLOW`` can release or hide thousands of postings, and that
should happen when the operator asks, having seen the number — not at 22:00 with
nobody watching.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated

import typer
from sqlalchemy import func, select

from scout_careers.cli._async import run as run_async
from scout_careers.cli.output import dash, echo, echo_json, echo_table
from scout_careers.common.config import Settings, get_settings
from scout_careers.common.logging import configure_logging
from scout_careers.db.models import Company, JobPosting
from scout_careers.db.session import session_scope
from scout_careers.ingest.filters import CHAIN, role_marker_hits
from scout_careers.ingest.screen import (
    ScreenOutcome,
    apply_screen,
    plan_screen,
    reason_families,
)

app = typer.Typer(no_args_is_help=True, help="Apply or inspect the stage ④ filter.")

#: Gate 2.6: the filter must remove at least this share of the corpus, or the
#: extraction budget is being spent on postings the operator would never open.
GATE_REMOVAL_PCT = Decimal(70)

#: Words that make a title an engineering title. Used only to *flag* a deny-list
#: candidate that would remove one — the `Full Stack Engineer - Internal Audit`
#: case that got `audit` rejected. Deliberately not a rule: "Data Center
#: Architect" contains none of these and is still not a role worth seeing.
ENGINEERING_WORDS = re.compile(
    r"\b(engineer|engineering|developer|programmer|sre|architect)\b", re.IGNORECASE
)


#: The keys whose defaults live in `config.py` and whose values decide what the
#: filter removes. Reported when the environment pins one, because a pinned key
#: makes an edit to the default inert.
_FILTER_KEYS: tuple[str, ...] = (
    "default_location_filter",
    "filter_seniority_deny",
    "filter_keyword_deny",
    "filter_role_marker_deny",
    "filter_role_marker_min",
    "filter_max_years_experience",
)


def _report_overrides(settings: Settings) -> None:
    """Name the filter keys the environment is pinning, if any.

    A silent, expensive failure otherwise. `.env` is usually seeded by copying
    `.env.example`, which pins *every* key — so a later change to a default in
    `config.py` has no effect at all, and the only symptom is a `filter apply`
    that reports "newly rejected 0" and looks like it had already been run.
    That happened: 34 deny-list entries added, 153 postings expected to move,
    zero moved, and the run said nothing about why.

    `model_fields_set` is exactly the question — which fields were supplied
    rather than defaulted — so this costs one attribute read and no guesswork.
    """
    pinned = sorted(key for key in _FILTER_KEYS if key in settings.model_fields_set)
    if not pinned:
        return
    echo("")
    echo(
        f"  {len(pinned)} filter key(s) are set in the environment and override "
        "the defaults in config.py. An edit to a default below will do nothing "
        "until the .env value is changed too:"
    )
    for key in pinned:
        echo(f"    {key.upper()}")


def _report(outcome: ScreenOutcome) -> None:
    """Print the summary and the per-predicate breakdown."""
    total = outcome.considered
    pct = Decimal(outcome.rejected * 100) / Decimal(total) if total else Decimal(0)

    echo_table(
        ["metric", "value"],
        [
            ["considered", f"{total:,}"],
            ["passing", f"{outcome.passing:,}"],
            ["rejected", f"{outcome.rejected:,}  ({pct:.1f}%)"],
            ["newly rejected", f"{outcome.newly_rejected:,}"],
            ["readmitted", f"{outcome.readmitted:,}"],
            ["left to dedup", f"{outcome.skipped_superseded:,}"],
            ["gate 2.6 (>=70%)", "PASS" if pct >= GATE_REMOVAL_PCT else "FAIL"],
        ],
    )

    # Chain order, not frequency: the shape of the filter is the point, and
    # first-rejection-wins means these do not overlap.
    order = {name: index for index, (name, _) in enumerate(CHAIN)}
    families = reason_families(outcome.by_reason)
    echo("")
    echo_table(
        ["predicate", "killed", "share"],
        [
            [
                name,
                f"{count:,}",
                f"{Decimal(count * 100) / Decimal(total):.1f}%" if total else "0%",
            ]
            for name, count in sorted(families.items(), key=lambda kv: order.get(kv[0], 99))
        ],
    )

    if outcome.readmitted:
        echo("")
        echo(
            f"  {outcome.readmitted:,} posting(s) were released. Each one is an "
            "extraction call the next backfill will pay for."
        )
    _report_overrides(get_settings())


@app.command("apply")
def apply(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report and write nothing.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Re-judge every posting and write ``filtered_out`` / ``filter_reason``.

    Idempotent: a posting whose verdict already matches is not written, so a
    second run reports zero changes. A posting that now passes has its flag
    cleared, which is what makes the deny lists safe to edit.
    """
    settings = get_settings()
    configure_logging(settings)

    async def work() -> tuple[ScreenOutcome, int]:
        async with session_scope() as session:
            outcome, changes = await plan_screen(session, settings)
            if dry_run or not changes:
                return outcome, 0
            if not yes:
                # The prompt is inside the transaction's scope but before any
                # write, so declining leaves the session untouched.
                echo("")
                confirmed = typer.confirm(
                    f"Write {len(changes):,} posting(s)?",
                    default=False,
                )
                if not confirmed:
                    echo("Nothing written.")
                    return outcome, 0
            written = await apply_screen(session, changes)
            await session.commit()
            return outcome, written

    outcome, written = run_async(work())
    if as_json:
        echo_json({**outcome.as_stats(), "written": written, "dry_run": dry_run})
        return
    _report(outcome)
    echo("")
    echo(f"Wrote {written:,} posting(s)." if written else "Wrote nothing.")


@app.command("status")
def status(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """What the database currently holds, without re-judging anything.

    The difference between this and ``apply --dry-run`` is the whole question:
    this reads the stored flags, that recomputes them. When they disagree, the
    filter has not been applied since the settings last changed.
    """
    settings = get_settings()
    configure_logging(settings)

    async def work() -> dict[str, object]:
        async with session_scope() as session:
            total = await session.scalar(select(func.count()).select_from(JobPosting)) or 0
            hidden = (
                await session.scalar(
                    select(func.count())
                    .select_from(JobPosting)
                    .where(JobPosting.filtered_out.is_(True))
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(JobPosting.filter_reason, func.count())
                    .where(JobPosting.filtered_out.is_(True))
                    .group_by(JobPosting.filter_reason)
                )
            ).all()
        stored = {str(reason or "?"): int(count) for reason, count in rows}
        return {"postings": total, "filtered_out": hidden, "visible": total - hidden, **stored}

    report = run_async(work())
    if as_json:
        echo_json(report)
        return
    echo_table(["metric", "value"], [[key, f"{value:,}"] for key, value in report.items()])


__all__ = ["GATE_REMOVAL_PCT", "app"]


@app.command("markers")
def markers(
    limit: Annotated[
        int, typer.Option("--limit", help="How many near-miss postings to name.")
    ] = 30,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the go-to-market marker distribution over the *surviving* corpus.

    ``FILTER_ROLE_MARKER_MIN`` is the one filter setting that cannot be tuned by
    reading the code. Raising it leaks go-to-market adverts into the extraction
    spend; lowering it silently removes a forward-deployed engineering role,
    which CONFIGURATION.md records as a mistake already made once. Neither cost
    is visible without counting.

    So this counts. It reports how many surviving postings sit at each hit
    total, and names the ones just below the threshold — the population that
    changing the threshold by one would move. A posting at zero hits that is
    obviously a marketing advert is the other finding, and a different fix: the
    marker list is missing a whole vocabulary rather than being set too high.
    """
    settings = get_settings()
    configure_logging(settings)
    threshold = settings.filter_role_marker_min

    async def work() -> tuple[dict[int, int], list[tuple[int, str, str, str]]]:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(JobPosting).where(
                        JobPosting.filtered_out.is_(False),
                        JobPosting.closed_at.is_(None),
                    )
                )
            ).scalars()
            histogram: dict[int, int] = {}
            near: list[tuple[int, str, str, str]] = []
            for row in rows:
                hits = role_marker_hits(row.description_text or "", settings)
                histogram[len(hits)] = histogram.get(len(hits), 0) + 1
                if hits:
                    near.append((len(hits), row.id, row.title, ", ".join(hits)))
            near.sort(key=lambda item: (-item[0], item[2]))
            return histogram, near

    histogram, near = run_async(work())
    surviving = sum(histogram.values())

    if as_json:
        echo_json(
            {
                "threshold": threshold,
                "surviving": surviving,
                "histogram": {str(hits): count for hits, count in sorted(histogram.items())},
                "near_misses": [
                    {"hits": hits, "posting_id": posting_id, "title": title, "markers": marks}
                    for hits, posting_id, title, marks in near[:limit]
                ],
            }
        )
        return

    echo_table(
        ["marker hits", "surviving postings"],
        [[str(hits), f"{count:,}"] for hits, count in sorted(histogram.items())],
    )
    echo("")
    echo(
        f"{surviving:,} posting(s) survive at FILTER_ROLE_MARKER_MIN={threshold}. "
        f"{histogram.get(0, 0):,} of them contain no go-to-market marker at all."
    )
    would_go = sum(count for hits, count in histogram.items() if hits >= threshold - 1)
    if threshold > 1:
        echo(
            f"  Lowering the threshold to {threshold - 1} would remove {would_go:,} more. "
            "Read the list below before deciding — that is the population it moves."
        )
    if near:
        echo("")
        echo("  Surviving postings that matched at least one marker:")
        for hits, posting_id, title, marks in near[:limit]:
            echo(f"    {hits}  {posting_id[-8:]}  {title[:44]:44}  {marks[:60]}")
        if len(near) > limit:
            echo(f"    ... and {len(near) - limit} more")
    echo("")
    echo(
        "  A posting with zero hits that is plainly not an engineering role is a "
        "different problem: the marker list is missing that vocabulary entirely, "
        "and lowering the threshold will not reach it."
    )


@app.command("survivors")
def survivors(
    company: Annotated[
        str | None, typer.Option("--company", help="Only this company slug.")
    ] = None,
    min_count: Annotated[
        int, typer.Option("--min-count", help="Hide title groups smaller than this.")
    ] = 1,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List the surviving titles the marker predicate does not touch.

    ``filter markers`` answers "is the threshold right". This answers the
    question underneath it: what is in the corpus that the markers cannot see
    at all. On the live corpus that population is 1,178 postings — roughly
    ₹2,360 of the next backfill — and it contains "Strategic Finance, GTM" and
    "People Analytics Lead — Recruiting", neither of which uses a single word
    from a sales-process vocabulary.

    Titles are grouped, because they repeat: one employer posts the same role
    in nine regions, and nine identical lines is nine chances to stop reading.
    A group of nine is also the more useful unit — a wrong title posted nine
    times is nine extraction calls, and worth a marker on its own.

    Read-only. Nothing here changes a verdict; it exists so the next change to
    the deny lists is made against titles rather than against a hunch.
    """
    settings = get_settings()
    configure_logging(settings)

    async def work() -> list[tuple[str, str, int]]:
        async with session_scope() as session:
            statement = (
                select(Company.slug, JobPosting.title, JobPosting.description_text)
                .join(Company, Company.id == JobPosting.company_id)
                .where(
                    JobPosting.filtered_out.is_(False),
                    JobPosting.closed_at.is_(None),
                )
            )
            if company:
                statement = statement.where(Company.slug == company)
            tally: dict[tuple[str, str], int] = {}
            for slug, title, description in await session.execute(statement):
                if role_marker_hits(description or "", settings):
                    continue
                key = (slug, title or "(untitled)")
                tally[key] = tally.get(key, 0) + 1
            return sorted(
                ((slug, title, count) for (slug, title), count in tally.items()),
                key=lambda row: (row[0], -row[2], row[1]),
            )

    rows = [row for row in run_async(work()) if row[2] >= min_count]
    total = sum(count for _, _, count in rows)

    if as_json:
        echo_json(
            {
                "postings": total,
                "groups": len(rows),
                "titles": [
                    {"company": slug, "title": title, "postings": count}
                    for slug, title, count in rows
                ],
            }
        )
        return

    echo_table(
        ["company", "n", "title"],
        [[slug, str(count), title[:70]] for slug, title, count in rows],
    )
    echo("")
    echo(
        f"{total:,} surviving posting(s) in {len(rows):,} title group(s) contain no "
        "go-to-market marker. Every one is an extraction call the next backfill pays for."
    )
    echo(
        "  A whole class of wrong roles here means the marker list is missing that "
        "vocabulary, not that the threshold is too high."
    )


#: Candidate title words drafted from the 1,178 surviving zero-marker postings.
#: Not configuration and not yet a deny list — the input to `filter try-titles`,
#: which is how an entry earns its place. Grouped by the class it came from so a
#: whole group can be dropped after reading what it costs.
CANDIDATE_TITLE_WORDS: tuple[str, ...] = (
    # finance, accounting and tax
    "accountant",
    "paralegal",
    "tax analyst",
    "fp&a",
    "strategic finance",
    "treasury",
    "payroll",
    "credit risk",
    "internal audit",
    "business controller",
    # people and talent
    "recruiting coordinator",
    "talent sourcer",
    "technical sourcer",
    "executive assistant",
    "executive business partner",
    "people analytics",
    "employee relations",
    "benefits analyst",
    "total rewards",
    "talent acquisition",
    # design and content
    "product designer",
    "brand designer",
    "motion designer",
    "web designer",
    "production designer",
    "content designer",
    "design systems lead",
    "copywriter",
    # hardware, silicon and datacentre
    "asic",
    "rtl design",
    "design verification",
    "pcba",
    "manufacturing engineer",
    "data center",
    "datacenter",
    "power trading",
    "power delivery",
)


@app.command("try-titles")
def try_titles(
    words: Annotated[
        str | None,
        typer.Option("--words", help="Comma-separated candidates; defaults to the draft list."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Sample titles to show per word.")] = 4,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Simulate title deny-list candidates against the surviving corpus.

    ``FILTER_KEYWORD_DENY`` earns entries by measurement, not by reading. Its
    docstring records the two that were tried and rejected — ``marketing`` and
    ``audit`` name an *org* rather than a role, and one in four of what they
    removed was a backend job serving that org ("Full Stack Engineer - Internal
    Audit"). ``solutions architect`` and ``strategist`` cost a role worth seeing
    each. This command is how the next candidate is held to the same standard.

    Whole-word, against the **title only**, exactly as the predicate matches.
    Anything a candidate would remove whose title also contains an engineering
    word is flagged rather than hidden: that is the ``Internal Audit`` failure,
    and it is the one worth catching before the list is edited rather than after.
    """
    settings = get_settings()
    configure_logging(settings)
    candidates = (
        tuple(word.strip().lower() for word in words.split(",") if word.strip())
        if words
        else CANDIDATE_TITLE_WORDS
    )

    async def work() -> list[tuple[str, list[str], list[str]]]:
        async with session_scope() as session:
            titles = list(
                (
                    await session.execute(
                        select(JobPosting.title).where(
                            JobPosting.filtered_out.is_(False),
                            JobPosting.closed_at.is_(None),
                        )
                    )
                ).scalars()
            )
            results: list[tuple[str, list[str], list[str]]] = []
            for word in candidates:
                pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
                hit = sorted(title for title in titles if title and pattern.search(title))
                suspect = [title for title in hit if ENGINEERING_WORDS.search(title)]
                results.append((word, hit, suspect))
            return results

    results = run_async(work())

    if as_json:
        echo_json(
            {
                "candidates": [
                    {"word": word, "removes": len(hit), "titles": hit, "suspect": suspect}
                    for word, hit, suspect in results
                ]
            }
        )
        return

    echo_table(
        ["candidate", "removes", "engineering-titled", "sample"],
        [
            [
                word,
                str(len(hit)),
                str(len(suspect)) if suspect else dash(None),
                "; ".join(title[:34] for title in hit[:2]),
            ]
            for word, hit, suspect in results
        ],
    )
    total = len({title for _, hit, _ in results for title in hit})
    flagged = [(word, suspect) for word, _, suspect in results if suspect]
    echo("")
    echo(f"{total:,} distinct surviving posting(s) would be removed by these {len(results)} words.")
    if flagged:
        echo("")
        echo(
            "  These candidates also match a title containing an engineering word. "
            "`marketing` and `audit` were rejected for exactly this — one in four "
            "of what they removed was a backend job serving that org:"
        )
        for word, suspect in flagged:
            echo(f"    {word}")
            for title in suspect[:limit]:
                echo(f"      {title[:76]}")
    echo("")
    echo(
        "  Nothing was written. Add the words that survive this reading to "
        "FILTER_KEYWORD_DENY, then `filter apply --dry-run`."
    )
