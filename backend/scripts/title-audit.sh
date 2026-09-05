#!/usr/bin/env bash
#
# What kind of roles survive stage ④, and what a longer deny-list would do.
# Writes nothing. Changes no configuration.
#
# The kill rate answers "how much was removed". This answers the question that
# actually matters: "is what remains worth a model call". The first live run
# removed 69.6% and left 2,191 postings — of which a large share were Account
# Executives, Solutions Architects and Counsel, because a six-word deny-list
# cannot describe the shape of a tech-company job board.
#
# Buckets are heuristics for *reading*, not rules the filter applies. Nothing
# here is enforced anywhere; it exists so a deny-list change can be argued from
# counts instead of from intuition.
#
# Usage:
#   bash scripts/title-audit.sh                       # audit as configured
#   bash scripts/title-audit.sh "account executive" "solutions architect"
#                                                     # ...and simulate additions

set -euo pipefail
cd "$(dirname "$0")/.."

EXTRA="$(IFS=,; echo "$*")"

EXTRA="$EXTRA" exec python - <<'PY'
import os
import re
from collections import Counter

from sqlalchemy import select

from scout_careers.cli._async import run as run_async
from scout_careers.common.config import get_settings
from scout_careers.db.models import Company, JobPosting
from scout_careers.db.session import session_scope
from scout_careers.ingest.filters import CompanyView, PostingView, evaluate

#: Title patterns for roles this operator is actually a candidate for. Whole
#: words, matched case-insensitively. This list is descriptive — it buckets the
#: survivors for reading and is not consulted by the filter.
ENGINEERING = (
    r"software engineer", r"backend", r"back.end", r"full.?stack", r"platform engineer",
    r"infrastructure engineer", r"site reliability", r"\bsre\b", r"devops",
    r"data engineer", r"machine learning", r"\bml\b", r"\bai\b engineer",
    r"ai engineer", r"applied scientist", r"research engineer",
    r"forward deployed", r"\bfde\b", r"systems engineer", r"api engineer",
    r"distributed systems", r"cloud engineer", r"security engineer",
    r"detection engineer", r"software development",
)

#: Go-to-market and customer-facing. Technical-sounding, commercially aimed.
GTM = (
    r"account executive", r"account manager", r"solutions architect",
    r"solutions engineer", r"sales engineer", r"customer success",
    r"customer engineer", r"technical support", r"support engineer",
    r"presales", r"pre.sales", r"business development", r"partner manager",
    r"strategist", r"strategic account", r"renewals", r"field engineer",
)

#: Corporate functions that appear on every large board.
CORPORATE = (
    r"counsel", r"legal", r"paralegal", r"audit", r"treasury", r"tax\b",
    r"marketing", r"communications", r"public affairs", r"external affairs",
    r"policy", r"recruit", r"people ops", r"human resources", r"\bhr\b",
    r"finance", r"accounting", r"procurement", r"facilities", r"workplace",
    r"designer", r"\bdesign\b", r"content", r"writer", r"program manager",
    r"product manager", r"project manager",
)

BUCKETS = (("engineering", ENGINEERING), ("go-to-market", GTM), ("corporate", CORPORATE))


def bucket_of(title: str) -> str:
    """Classify one title. Engineering wins ties — a 'Security Engineer' in a
    corporate-sounding org is still an engineering role."""
    lowered = title.lower()
    for name, patterns in BUCKETS:
        if any(re.search(pattern, lowered) for pattern in patterns):
            return name
    return "unclassified"


def denied_by(title: str, entries: list[str]) -> str | None:
    """The most specific deny entry matching this title, or None."""
    lowered = title.lower()
    for needle in sorted(entries, key=lambda e: (-len(e), e)):
        if needle and re.search(rf"\b{re.escape(needle)}\b", lowered):
            return needle
    return None


async def main() -> None:
    settings = get_settings()
    # Joined with commas by the wrapper, because shell word-splitting would
    # turn "account executive" into two useless single-word entries.
    extra = [e.strip().lower() for e in os.environ.get("EXTRA", "").split(",") if e.strip()]

    survivors: list[str] = []
    async with session_scope() as session:
        rows = await session.stream(
            select(JobPosting, Company).join(Company, Company.id == JobPosting.company_id)
        )
        async for posting, company in rows:
            verdict = evaluate(
                PostingView(
                    id=posting.id,
                    title=posting.title,
                    description_text=posting.description_text or "",
                    location_city=posting.location_city,
                    location_country=posting.location_country,
                    is_remote=posting.is_remote,
                    seniority_guess=posting.seniority_guess,
                    closed_at=posting.closed_at,
                    filtered_out=posting.filtered_out,
                    filter_reason=posting.filter_reason,
                    raw=posting.raw or {},
                ),
                CompanyView(
                    id=company.id,
                    slug=company.slug,
                    status=company.status,
                    location_filter=company.location_filter or (),
                ),
                settings,
            )
            if verdict.passed:
                survivors.append(posting.title)

    counts = Counter(bucket_of(title) for title in survivors)
    total = len(survivors)

    print()
    print(f"  {total:,} postings survive stage ④ as configured. What they are:")
    print()
    print(f"  {'bucket':<16} {'count':>7}  {'share':>7}")
    for name in ("engineering", "go-to-market", "corporate", "unclassified"):
        n = counts[name]
        print(f"  {name:<16} {n:>7,}  {n * 100 / total if total else 0:>6.1f}%")
    print()

    for name in ("go-to-market", "corporate"):
        titles = [t for t in survivors if bucket_of(t) == name]
        print(f"  {name}, a sample of what you would be paying to extract:")
        for title in titles[:: max(1, len(titles) // 8)][:8]:
            print(f"    {title[:74]}")
        print()

    print("  unclassified — read these carefully, they are the ones a longer")
    print("  deny-list would silently take with it:")
    unknown = [t for t in survivors if bucket_of(t) == "unclassified"]
    for title in unknown[:: max(1, len(unknown) // 12)][:12]:
        print(f"    {title[:74]}")
    print()

    if extra:
        print(f"  simulating {len(extra)} additional deny entries: {', '.join(extra)}")
        would_drop = [(t, d) for t in survivors if (d := denied_by(t, extra))]
        by_entry = Counter(d for _, d in would_drop)
        collateral = [t for t, _ in would_drop if bucket_of(t) == "engineering"]
        remaining = total - len(would_drop)
        print()
        print(f"  {'entry':<26} {'drops':>7}")
        for entry, n in by_entry.most_common():
            print(f"  {entry:<26} {n:>7,}")
        print()
        print(f"  survivors {total:,} -> {remaining:,}")
        print(f"  ENGINEERING ROLES LOST: {len(collateral)}")
        for title in collateral[:10]:
            print(f"    !! {title[:70]}")
        print()
        print("  A non-zero collateral count is the number that should decide this.")
        print("  A role dropped here is one you never see and never learn you missed.")
        print()


run_async(main())
PY
