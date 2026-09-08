#!/usr/bin/env bash
#
# What extraction actually stored. Read-only, and it uses the app's own session
# so there is no container name to find.
#
# The number that matters is the resolution rate. `requirement.normalised_skill`
# is what coverage scoring intersects against `resume_variant.skill_set`, so an
# unresolved requirement is one scoring counts as unmatched — it understates
# coverage, which is the safe direction to be wrong in, but only up to a point.
# Below roughly half resolved, the score is measuring the vocabulary's gaps more
# than the operator's fit.
#
# The unresolved phrases are the useful output: they are the proposal queue that
# `skill_proposal` will one day be, printed by frequency so the next vocabulary
# commit is a decision about data rather than a guess.
#
# EVERYTHING BELOW THE SUMMARY IS SCOPED TO ONE prompt_version. A corpus part-way
# through a re-extraction holds two or three generations at once, and averaging
# over them measures neither: after `2026-09-06.3` taught the model to split
# multi-skill lines, a blended rate mixed split rows with unsplit ones and moved
# two points, which says nothing about whether the change worked. `prompt_version`
# exists to keep generations apart; a report that ignores it throws away the one
# thing that makes the comparison possible.
#
# Defaults to the current prompt + vocabulary — the version the next backfill
# will write.
#
# Usage:  bash scripts/requirements-audit.sh [sample_postings] [prompt_version|all]

set -euo pipefail
cd "$(dirname "$0")/.."

SAMPLE="${1:-3}" VERSION="${2:-}" exec python - <<'PY'
import os
from collections import Counter

from sqlalchemy import func, select

from scout_careers.cli._async import run as run_async
from scout_careers.common.types import SCORED_REQUIREMENT_KINDS
from scout_careers.db.models import JobPosting, Requirement
from scout_careers.db.session import session_scope
from scout_careers.extract.service import FAMILY, version_string
from scout_careers.extract.vocabulary import canonical_key, get_vocabulary
from scout_careers.llm.registry import PromptRegistry

SCORED = sorted(k.value for k in SCORED_REQUIREMENT_KINDS)


async def main() -> None:
    sample = int(os.environ.get("SAMPLE", "3"))
    vocab = get_vocabulary()
    wanted = os.environ.get("VERSION") or version_string(PromptRegistry().get(FAMILY), vocab)
    scope = () if wanted == "all" else (Requirement.prompt_version == wanted,)

    async with session_scope() as session:
        # The whole table first, by version, so a blend is visible rather than
        # silently averaged into every number that follows.
        generations = (
            await session.execute(
                select(
                    Requirement.prompt_version,
                    func.count(func.distinct(Requirement.posting_id)),
                    func.count(),
                )
                .group_by(Requirement.prompt_version)
                .order_by(Requirement.prompt_version)
            )
        ).all()

        total = await session.scalar(
            select(func.count()).select_from(Requirement).where(*scope)
        ) or 0
        if not total:
            print()
            print(f"  Nothing stored at {wanted}.")
            if generations:
                print("  Versions present:")
                for version, posts, reqs in generations:
                    print(f"    {version:<56} {posts:>5} posting(s), {reqs:>6,} requirement(s)")
                print()
                print("  Re-extract at the current version, or pass one of the above:")
                print("    scout-careers extract postings --limit 30 --force")
            else:
                print("  Run `scout-careers extract postings`.")
            print()
            return

        postings = await session.scalar(
            select(func.count(func.distinct(Requirement.posting_id))).where(*scope)
        ) or 0
        resolved = await session.scalar(
            select(func.count())
            .select_from(Requirement)
            .where(Requirement.normalised_skill.is_not(None), *scope)
        ) or 0

        # The rate that matters is over `hard` and `nice` only. Those are the
        # kinds coverage scoring weighs; `responsibility` is a whole sentence
        # about what the job involves ("Triage support cases, directing users
        # to previous answers") and was never going to resolve to a skill
        # token. Counting them makes the vocabulary look far worse than it is,
        # and would send the next commit chasing aliases nothing should match.
        scored = await session.scalar(
            select(func.count())
            .select_from(Requirement)
            .where(Requirement.kind.in_(SCORED), *scope)
        ) or 0
        scored_resolved = await session.scalar(
            select(func.count())
            .select_from(Requirement)
            .where(
                Requirement.kind.in_(SCORED), Requirement.normalised_skill.is_not(None), *scope
            )
        ) or 0

        by_kind = (
            await session.execute(
                select(Requirement.kind, func.count()).where(*scope).group_by(Requirement.kind)
            )
        ).all()

        top_skills = (
            await session.execute(
                select(Requirement.normalised_skill, func.count())
                .where(Requirement.normalised_skill.is_not(None), *scope)
                .group_by(Requirement.normalised_skill)
                .order_by(func.count().desc())
                .limit(20)
            )
        ).all()

        # Only the scored kinds. An unresolved `responsibility` is not a gap in
        # the vocabulary, so listing it here would fill the proposal queue with
        # entries nobody should act on.
        unresolved = (
            await session.execute(
                select(Requirement.text_).where(
                    Requirement.normalised_skill.is_(None),
                    Requirement.kind.in_(SCORED),
                    *scope,
                )
            )
        ).scalars().all()

        recent = (
            await session.execute(
                select(Requirement.posting_id).where(*scope).distinct().limit(sample)
            )
        ).scalars().all()

        rows = (
            await session.execute(
                select(JobPosting.title, Requirement)
                .join(JobPosting, JobPosting.id == Requirement.posting_id)
                .where(Requirement.posting_id.in_(list(recent)), *scope)
                .order_by(Requirement.posting_id, Requirement.ordinal)
            )
        ).all()

    print()
    print(f"  prompt_version       {wanted}")
    if len(generations) > 1:
        print("  other generations still stored (not counted below):")
        for version, posts, reqs in generations:
            if version != wanted:
                print(f"    {version:<56} {posts:>4} posting(s), {reqs:>6,} requirement(s)")
    print()
    print(f"  postings extracted   {postings:>6,}")
    print(f"  requirements         {total:>6,}   {total / postings:.1f} per posting")
    print(f"  resolved to a token  {resolved:>6,}   {resolved * 100 / total:.0f}% of all")
    if scored:
        print(
            f"  ... of hard + nice   {scored_resolved:>6,}   "
            f"{scored_resolved * 100 / scored:.0f}% of {scored:,}   <- the rate that matters"
        )
    print(f"  vocabulary           {len(vocab.tokens)} tokens, {len(vocab.alias_index)} aliases")
    print()
    print("  by kind (only hard and nice carry coverage weight)")
    for kind, count in sorted(by_kind, key=lambda kv: -kv[1]):
        print(f"    {str(kind.value if hasattr(kind, 'value') else kind):<16} {count:>5,}")

    print()
    print("  most demanded skills")
    for skill, count in top_skills:
        print(f"    {skill:<24} {count:>5,}")

    # The proposal queue. Grouped on the canonical key so "5+ years of Python"
    # and "5+ years of python" are one line, not two.
    print()
    print(f"  unresolved hard/nice phrases ({len(unresolved):,}), most common first")
    groups: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for text in unresolved:
        key = canonical_key(text)
        groups[key] += 1
        examples.setdefault(key, text)
    for key, count in groups.most_common(30):
        print(f"    {count:>3}x  {examples[key][:70]}")
    if len(groups) > 30:
        print(f"    ... and {len(groups) - 30:,} more distinct phrases")

    print()
    print(f"  a sample of {sample} posting(s), as stored")
    current = None
    for title, requirement in rows:
        if requirement.posting_id != current:
            current = requirement.posting_id
            print()
            print(f"    {title[:74]}")
        skill = requirement.normalised_skill or "-"
        print(
            f"      {requirement.ordinal:>2}. [{requirement.kind.value:<14}] "
            f"{skill:<20} w={requirement.weight}  {requirement.text_[:60]}"
        )
    print()


run_async(main())
PY
