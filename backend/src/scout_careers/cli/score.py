"""``scout-careers score`` — run stage ⑥ over the extracted corpus.

Costs nothing. Scoring is set arithmetic over rows already in the database, so
unlike ``extract`` there is no estimate command and no budget breaker: six
variants against every extracted posting is a few seconds of Decimal work.

Three commands. ``postings`` scores and stores; ``show`` explains one posting's
scores without touching them; ``gaps`` answers the question §8.2 says no job
board will — which single missing skill blocks the most roles that would
otherwise match.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import typer
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_careers.cli._async import run as run_async
from scout_careers.cli.output import dash, echo, echo_json, echo_table, error
from scout_careers.common.config import Settings, get_settings
from scout_careers.common.logging import configure_logging
from scout_careers.common.types import CompanyTier, CoverageLevel
from scout_careers.db.models import Company, JobPosting, MatchScore, Requirement
from scout_careers.db.session import session_scope
from scout_careers.scoring.adjacency import get_adjacency
from scout_careers.scoring.gaps import blocking_gaps, build_gaps
from scout_careers.scoring.service import (
    PostingScored,
    load_requirements,
    load_variants,
    plan_posting,
    score_version,
    store_scores,
)

app = typer.Typer(no_args_is_help=True, help="Score postings against the resume variants.")

#: Deterministic arithmetic, no model call. Recorded rather than left blank
#: because ``match_score.model`` is NOT NULL and invariant 7 says nothing this
#: system produces may be unable to name what produced it — "none" is a true
#: answer and a silently empty string is not.
MODEL = "deterministic"


def _scoreable() -> Any:
    """Open, unfiltered postings that have requirements extracted.

    Closed postings are never rescored (§11.1): their historical scores are what
    make them usable as evaluation data later.
    """
    return (
        select(JobPosting)
        .where(
            JobPosting.closed_at.is_(None),
            JobPosting.filtered_out.is_(False),
            exists().where(Requirement.posting_id == JobPosting.id),
        )
        .order_by(JobPosting.first_seen_at.desc())
    )


async def _score_one(
    session: AsyncSession,
    posting: JobPosting,
    variants: list[Any],
    *,
    tier: CompanyTier,
    default_variant_id: int | None,
    settings: Settings,
    now: datetime,
) -> PostingScored:
    requirements = await load_requirements(session, posting.id)
    return plan_posting(
        requirements,
        variants,
        posting_id=posting.id,
        tier=tier,
        # `coalesce(posted_at, first_seen_at)`: a board that omits the date is
        # not a reason to treat a posting as ageless.
        posted_at=posting.posted_at or posting.first_seen_at,
        now=now,
        settings=settings,
        default_variant_id=default_variant_id,
    )


async def _run(limit: int | None, as_json: bool, dry_run: bool) -> int:
    settings = get_settings()
    adjacency = get_adjacency()
    version = score_version(settings, adjacency)
    now = datetime.now(UTC)

    async with session_scope() as session:
        variants = await load_variants(session)
        if not variants:
            error("No resume variants are seeded. Run `scout-careers seed variants` first.")
            return 1

        statement = _scoreable()
        if limit is not None:
            statement = statement.limit(limit)
        postings = list((await session.execute(statement)).scalars())

        tiers = {
            row.id: (row.tier, row.default_variant_id)
            for row in (await session.execute(select(Company))).scalars()
        }

        rows: list[dict[str, Any]] = []
        flagged: list[str] = []
        written = 0

        for posting in postings:
            tier, default_variant_id = tiers.get(posting.company_id, (CompanyTier.VOLUME, None))
            scored = await _score_one(
                session,
                posting,
                variants,
                tier=tier,
                default_variant_id=default_variant_id,
                settings=settings,
                now=now,
            )
            if scored.winner is None:
                flagged.append(f"{posting.id}  {scored.note}")
                continue

            if not dry_run:
                written += await store_scores(
                    session, scored, prompt_version=version, model=MODEL, now=now
                )

            winner = scored.winner
            rows.append(
                {
                    "posting_id": posting.id,
                    "title": posting.title,
                    "variant": winner.variant.key,
                    "coverage_pct": str(winner.result.coverage_pct),
                    "composite": str(winner.result.composite_score),
                    "hard": f"{len(winner.hard) - winner.hard_missing}/{len(winner.hard)}",
                    "blocking": len(blocking_gaps(build_gaps(winner.outcomes))),
                    "recency": str(winner.result.recency),
                    "gate": str(winner.result.hard_gate),
                }
            )

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    if as_json:
        echo_json({"version": version, "written": written, "flagged": flagged, "postings": rows})
        return 0

    rows.sort(key=lambda row: float(row["composite"]), reverse=True)
    echo_table(
        ["posting", "title", "variant", "coverage", "composite", "hard met", "blockers"],
        [
            [
                row["posting_id"][-8:],
                str(row["title"])[:44],
                str(row["variant"]),
                str(row["coverage_pct"]),
                str(row["composite"]),
                str(row["hard"]),
                str(row["blocking"]),
            ]
            for row in rows[:40]
        ],
    )
    echo()
    verb = "would write" if dry_run else "wrote"
    echo(f"{len(rows)} posting(s) scored at {version}; {verb} {written} match_score row(s).")
    if len(rows) > 40:
        echo(f"  showing the top 40 by composite; {len(rows) - 40} more scored.")
    if flagged:
        echo()
        echo(
            f"  {len(flagged)} posting(s) could not be scored and are flagged for "
            "manual review rather than scored as a perfect match:"
        )
        for line in flagged[:10]:
            echo(f"    {line}")
        if len(flagged) > 10:
            echo(f"    ... and {len(flagged) - 10} more")
    return 0


@app.command("postings")
def postings(
    limit: Annotated[
        int | None, typer.Option("--limit", help="Score at most this many postings.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Compute and report, write nothing.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Score every open, extracted posting against every variant.

    Re-runnable and idempotent within a version: a rescore under the same
    ``prompt_version`` replaces its own rows. Bump ``SCORING_PROMPT_VERSION`` and
    the new family lands beside the old one instead, which is what makes the A/B
    comparison in MATCH_SCORING.md §11.3 possible.
    """
    settings = get_settings()
    configure_logging(settings)
    raise SystemExit(run_async(_run(limit, as_json, dry_run)))


async def _show(posting_id: str, as_json: bool) -> int:
    settings = get_settings()
    version = score_version(settings, get_adjacency())

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MatchScore)
                    .where(
                        MatchScore.posting_id == posting_id,
                        MatchScore.prompt_version == version,
                    )
                    .order_by(MatchScore.composite_score.desc())
                )
            ).scalars()
        )
        if not rows:
            error(f"No scores for {posting_id} at {version}. Run `score postings` first.")
            return 1

        names = {variant.id: variant.key for variant in await load_variants(session)}
        winner = next((row for row in rows if row.is_recommended), rows[0])
        winner_key = names.get(winner.variant_id, str(winner.variant_id))
        table = [
            [
                names.get(row.variant_id, str(row.variant_id)),
                str(row.coverage_pct),
                str(row.composite_score),
                f"{row.hard_met}/{row.hard_total}",
                f"{row.nice_met}/{row.nice_total}",
                "yes" if row.is_recommended else dash(None),
            ]
            for row in rows
        ]
        winner_gaps = list(winner.gaps)
        winner_evidence = list(winner.evidence)
        payload = [
            {
                "variant": names.get(row.variant_id, str(row.variant_id)),
                "coverage_pct": str(row.coverage_pct),
                "composite": str(row.composite_score),
                "hard": f"{row.hard_met}/{row.hard_total}",
                "nice": f"{row.nice_met}/{row.nice_total}",
                "recommended": row.is_recommended,
                "gaps": list(row.gaps),
                "evidence": list(row.evidence),
            }
            for row in rows
        ]

    if as_json:
        echo_json({"posting_id": posting_id, "version": version, "scores": payload})
        return 0

    echo_table(["variant", "coverage", "composite", "hard met", "nice met", "recommended"], table)

    if winner_gaps:
        echo()
        echo(f"  Gaps for {winner_key}, worst first:")
        for gap in winner_gaps[:12]:
            note = f"  — {gap['note']}" if gap.get("note") else ""
            echo(f"    [{gap['kind']}/{gap['level']}] {str(gap['text'])[:80]}{note}")
    if winner_evidence:
        echo()
        echo(f"  Evidence for {winner_key}:")
        for item in winner_evidence[:12]:
            claims = ", ".join(item.get("claim_keys") or []) or "no claims cited"
            echo(f"    {str(item['requirement'])[:60]}")
            echo(f"      {item['bullet_ref']}  ({claims})")
    return 0


@app.command("show")
def show(
    posting_id: Annotated[str, typer.Argument(help="The posting to explain.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Explain one posting's scores: every variant, the winner's gaps and evidence."""
    settings = get_settings()
    configure_logging(settings)
    raise SystemExit(run_async(_show(posting_id, as_json)))


async def _gap_report(top: int, as_json: bool) -> int:
    """Which single missing skill blocks the most otherwise-matching roles."""
    settings = get_settings()
    version = score_version(settings, get_adjacency())

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MatchScore).where(
                        MatchScore.prompt_version == version,
                        MatchScore.is_recommended.is_(True),
                    )
                )
            ).scalars()
        )

    tally: dict[tuple[str, str], int] = {}
    for row in rows:
        for gap in row.gaps:
            if gap.get("level") != CoverageLevel.MISSING.value:
                continue
            key = (str(gap.get("kind")), str(gap.get("text"))[:70])
            tally[key] = tally.get(key, 0) + 1

    ranked = sorted(tally.items(), key=lambda item: (-item[1], item[0][1]))[:top]

    if as_json:
        echo_json(
            {
                "version": version,
                "postings": len(rows),
                "gaps": [
                    {"kind": kind, "text": text, "postings": count}
                    for (kind, text), count in ranked
                ],
            }
        )
        return 0

    echo_table(
        ["postings", "kind", "requirement"],
        [[str(count), kind, text] for (kind, text), count in ranked],
    )
    echo()
    echo(
        f"Across {len(rows)} recommended score(s) at {version}. A requirement high "
        "in this list is a week of learning with a measurable return, and the "
        "measurement is the rescore."
    )
    return 0


@app.command("gaps")
def gaps(
    top: Annotated[int, typer.Option("--top", help="How many rows to show.")] = 25,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Rank missing requirements by how many recommended matches they block."""
    settings = get_settings()
    configure_logging(settings)
    raise SystemExit(run_async(_gap_report(top, as_json)))


__all__ = ["MODEL", "app"]
