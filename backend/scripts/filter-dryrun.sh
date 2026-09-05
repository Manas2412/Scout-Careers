#!/usr/bin/env bash
#
# What stage ④ would do to the postings already in the database. Writes nothing.
#
# Gate 2.6 asks for two things: the filter removes >= 70% of postings, and it
# issues zero model calls. The second is true by construction — this chain is
# pure boolean logic with no client. The first is a claim about *your* corpus
# and your filter settings, and the only way to know it is to run it.
#
# The per-predicate breakdown is the useful half. A chain that kills 75% tells
# you gate 2.6 passes; a chain where 74 of those 75 points come from one
# predicate tells you the filter is really one rule and the rest is decoration.
#
# Usage:  bash scripts/filter-dryrun.sh

set -euo pipefail
cd "$(dirname "$0")/.."

exec python - <<'PY'
import asyncio
import re
from collections import Counter
from decimal import Decimal

from sqlalchemy import select

from scout_careers.common.config import get_settings
from scout_careers.cli._async import run as run_async
from scout_careers.db.models import Company, JobPosting
from scout_careers.db.session import session_scope
from scout_careers.ingest.filters import (
    CHAIN,
    CompanyView,
    PostingView,
    evaluate,
    parse_experience_years,
)

#: Words that appear in every title and say nothing about the role.
_STOP = {
    "and", "the", "for", "with", "our", "you", "are", "engineer", "senior",
    "staff", "lead", "sr.", "iii", "ii", "new", "grad",
}


async def main() -> None:
    settings = get_settings()
    reasons: Counter[str] = Counter()
    survivor_titles: list[str] = []
    # Every posting's stated requirement, regardless of verdict, so the ceiling
    # can be chosen from the corpus rather than guessed.
    years_seen: Counter[object] = Counter()
    passed = total = 0

    async with session_scope() as session:
        rows = await session.stream(
            select(JobPosting, Company).join(Company, Company.id == JobPosting.company_id)
        )
        async for posting, company in rows:
            total += 1
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
            years_seen[parse_experience_years(posting.description_text or "")] += 1
            if verdict.passed:
                passed += 1
                survivor_titles.append(posting.title)
            else:
                # Group `seniority:director` under `seniority`, keeping the
                # detail for the second table.
                reasons[verdict.reason or "?"] += 1

    removed = total - passed
    pct = Decimal(removed * 100) / Decimal(total) if total else Decimal(0)

    print()
    print(f"  postings          {total:>7,}")
    print(f"  survive           {passed:>7,}")
    print(f"  removed           {removed:>7,}   {pct:.1f}%")
    print(f"  gate 2.6 (>=70%)  {'PASS' if pct >= 70 else 'FAIL':>7}")
    print()

    # By predicate, in chain order, so the shape of the filter is visible.
    families: Counter[str] = Counter()
    for reason, count in reasons.items():
        families[reason.split(":", 1)[0]] += count
    order = {name: i for i, (name, _) in enumerate(CHAIN)}
    alias = {
        "company_blacklisted": "company_status",
        "description_too_short": "no_description",
        "no_description": "no_description",
        "title_keyword": "title_denylist",
        "location_excluded": "location",
    }
    print("  by predicate (first rejection wins, so these do not overlap)")
    print(f"  {'predicate':<20} {'killed':>8}  {'share':>7}")
    grouped: Counter[str] = Counter()
    for family, count in families.items():
        grouped[alias.get(family, family)] += count
    for name, count in sorted(
        grouped.items(), key=lambda kv: order.get(kv[0], 99)
    ):
        share = Decimal(count * 100) / Decimal(total) if total else Decimal(0)
        print(f"  {name:<20} {count:>8,}  {share:>6.1f}%")

    print()
    print("  most common exact reasons")
    for reason, count in reasons.most_common(12):
        print(f"  {reason:<32} {count:>7,}")
    print()


    # What got through. The kill rate says how much was removed; this says
    # whether what remains is worth a model call, which is the actual question.
    # What the corpus actually asks for. The ceiling should be read off this.
    stated = {k: v for k, v in years_seen.items() if k is not None}
    unstated = years_seen.get(None, 0)
    print("  stated experience requirement across ALL postings")
    print(f"    not stated              {unstated:>6,}  ({unstated * 100 / total:.0f}%)")
    running = 0
    for years in sorted(stated):
        running += stated[years]
        cut = sum(v for k, v in stated.items() if k > years)
        print(
            f"    {years:>2} year(s)              {stated[years]:>6,}"
            f"   a ceiling here would drop {cut:,}"
        )
    print()

    print("  surviving titles, most common head-words")
    words: Counter[str] = Counter()
    for title in survivor_titles:
        for word in re.findall(r"[a-z][a-z+#/.]{2,}", title.lower()):
            if word not in _STOP:
                words[word] += 1
    for word, count in words.most_common(25):
        print(f"    {word:<24} {count:>6,}")
    print()
    print("  a sample of survivors")
    for title in survivor_titles[:: max(1, len(survivor_titles) // 20)][:20]:
        print(f"    {title[:78]}")
    print()
    surviving_cost_in = passed * 4000 / 1_000_000 * float(settings.llm_price_fast_in)
    surviving_cost_out = passed * 600 / 1_000_000 * float(settings.llm_price_fast_out)
    usd = surviving_cost_in + surviving_cost_out
    print(
        f"  one full extraction pass over the survivors, at the configured "
        f"`fast` prices and 4k in / 600 out per posting:"
    )
    print(f"    ~${usd:,.2f}  ~Rs {usd * float(settings.llm_inr_per_usd):,.0f}")
    print("  (an estimate from assumed token counts; the real number comes from")
    print("   the `usage` block once the LLM layer is measuring it)")
    print()


run_async(main())
PY
