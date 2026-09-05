"""Stage ②a: deciding which company a mail-alert lead belongs to.

Every other adapter's postings belong to the company that owns the source: a
Greenhouse board token *is* an employer, so ``source.company_id`` is the answer
and ``persist`` denormalises it onto every row without asking. A mail alert is
not like that. One LinkedIn digest carries roles at forty employers, and the
source is a mailbox. Attaching those forty roles to the mailbox's own company
row would be wrong in a way that is quietly destructive rather than merely
untidy, because the cross-source dedup key is
``(company_id, normalise_title(title), location_city)``
(:mod:`~scout_careers.ingest.dedup`): with every alert row sharing one
``company_id``, two different employers' "Software Engineer" in Bengaluru
collapse into one posting and one of them disappears.

The 50-point fidelity gap does not save this. It orders a mail-alert row below a
real board row for *the same* company, which is exactly right; it says nothing
about two alert rows that only look like duplicates because this module had not
run.

So: SOURCE_ADAPTERS.md §7.3, trigram similarity against ``company.name`` at
``settings.alert_company_match_threshold`` (0.45), and a reserved ``unmatched``
company for everything below it. Unmatched is deliberately a row and not a
``NULL``: the operator's most valuable path into the registry is seeing a lead
at a company they do not yet track and adding its real board, and a dropped row
cannot be seen (COMPANY_REGISTRY.md §8).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scout_careers.common.logging import get_logger
from scout_careers.common.types import CompanyStatus, CompanyTier
from scout_careers.db.models import Company
from scout_careers.sources.alerts.entry import normalise_company
from scout_careers.sources.base import RawPosting

log = get_logger(__name__)

#: Where the ``mail_alert`` adapter parks the employer name it parsed.
#: ``RawPosting`` has no company field on purpose — resolution is this module's
#: job, not an adapter's — so the value travels in ``raw``.
ALERT_COMPANY_KEY: Final = "company_name"

#: Written onto every mail-sourced row. Stage ⑤ skips extraction on these: the
#: description is a five-line stub, and running the extractor over it would
#: produce confident, worthless requirements and then score against them, which
#: is worse than not scoring at all (SOURCE_ADAPTERS.md §7.3).
NEEDS_DESCRIPTION_KEY: Final = "needs_description"

#: The raw-column additions for a mail-sourced posting.
ALERT_RAW_FLAGS: Final[Mapping[str, Any]] = {NEEDS_DESCRIPTION_KEY: True}

#: The reserved holding row. One slug, referenced from here alone, so that
#: "is this the unmatched company" is a question with a single answer.
UNMATCHED_SLUG: Final = "unmatched"
UNMATCHED_NAME: Final = "Unmatched (mail alerts)"

#: ``company.tags`` is ``axis:value`` over a closed set of axes
#: (COMPANY_REGISTRY.md §5.1). This row is written by ``INSERT ... ON CONFLICT``
#: rather than through ``registry.create_company``, so ``validate_tags`` does not
#: run on it — which is exactly how an invalid tag would reach the column
#: unnoticed. ``test_the_reserved_row_s_tags_are_valid`` calls the validator on
#: this constant so the rule still holds without ``ingest/`` importing
#: ``registry/``.
UNMATCHED_TAGS: Final[tuple[str, ...]] = ("origin:mail-alert",)


@dataclass(frozen=True, slots=True)
class CompanyMatch:
    """A resolved company and the score that resolved it.

    Attributes:
        company_id: The matched row.
        name: Its registered name, for the log line — the operator reading
            "Acme Corp → Acme Technologies (0.52)" can tell a good match from a
            lucky one, which a bare id does not permit.
        score: The trigram similarity that cleared the threshold.
    """

    company_id: int
    name: str
    score: float


async def get_unmatched_company_id(session: AsyncSession) -> int:
    """Return the reserved ``unmatched`` company, creating it on first need.

    Args:
        session: The session this source's work runs in.

    Returns:
        Its id.

    The insert is ``ON CONFLICT DO NOTHING`` on ``slug`` rather than a
    select-then-insert: sources run concurrently, and two alert sources arriving
    together would otherwise race to create the same reserved row and one would
    take a unique-violation that rolls back a whole board's postings.
    """
    stmt = (
        insert(Company)
        .values(
            slug=UNMATCHED_SLUG,
            name=UNMATCHED_NAME,
            tier=CompanyTier.VOLUME,
            status=CompanyStatus.TRACKING,
            tags=list(UNMATCHED_TAGS),
            # No board is ever polled for this row, and no letter is ever
            # written for a lead whose employer we could not even identify.
            cover_letter_worth=False,
            notes=(
                "Reserved. Holds mail-alert leads whose parsed employer name did "
                "not match a tracked company. Promote a lead by adding its real "
                "board to the registry."
            ),
        )
        .on_conflict_do_nothing(index_elements=[Company.slug])
    )
    await session.execute(stmt)
    found = await session.execute(select(Company.id).where(Company.slug == UNMATCHED_SLUG))
    return int(found.scalar_one())


async def match_company(
    session: AsyncSession,
    name: str,
    *,
    threshold: float,
) -> CompanyMatch | None:
    """Find the tracked company a parsed alert name refers to.

    Args:
        session: The session this source's work runs in.
        name: The employer name as the alert parser read it, untrusted text.
        threshold: ``settings.alert_company_match_threshold``.

    Returns:
        The best match at or above the threshold, or ``None``.

    ``similarity(...) >= :threshold`` is used rather than the ``%`` operator.
    ``%`` is the form that can use ``company_name_trgm_idx``, but it takes its
    cut-off from the ``pg_trgm.similarity_threshold`` GUC — a per-session
    setting, which would make this function's behaviour depend on connection
    state rather than on its argument. At registry scale (hundreds of rows) the
    scan is not measurable, and the explicit comparison is the one a reader can
    reason about.
    """
    score = func.similarity(Company.name, name)
    rows = await session.execute(
        select(Company.id, Company.name, score.label("score"))
        .where(
            Company.deleted_at.is_(None),
            Company.slug != UNMATCHED_SLUG,
            score >= threshold,
        )
        # id ascending is the tie-break, so two equally-similar names resolve
        # the same way on every run instead of alternating.
        .order_by(score.desc(), Company.id.asc())
        .limit(1)
    )
    row = rows.first()
    if row is None:
        return None
    return CompanyMatch(company_id=int(row.id), name=row.name, score=float(row.score))


def alert_company_names(postings: Iterable[RawPosting]) -> dict[str, list[str]]:
    """Group a fetch's postings by the normalised employer name they carry.

    Args:
        postings: One ``mail_alert`` source's output.

    Returns:
        Normalised name → the ``external_id``s that carried it. Grouping first
        is what keeps a 200-lead digest to one query per distinct employer
        instead of one per lead, and ``normalise_company`` is the same function
        the adapter used to build the synthetic id, so "Acme Corp" and "Acme
        Corporation Pvt Ltd" are one group here exactly as they are one posting
        there.
    """
    grouped: dict[str, list[str]] = {}
    for posting in postings:
        raw_name = posting.raw.get(ALERT_COMPANY_KEY)
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        key = normalise_company(raw_name)
        if not key:
            continue
        grouped.setdefault(key, []).append(posting.external_id)
    return grouped


async def resolve_alert_companies(
    session: AsyncSession,
    postings: Iterable[RawPosting],
    *,
    threshold: float,
    source_id: int,
) -> dict[str, int]:
    """Map each mail-alert posting to the company it should be stored under.

    Args:
        session: The session this source's work runs in.
        postings: The source's complete buffered output.
        threshold: ``settings.alert_company_match_threshold``.
        source_id: For the log lines.

    Returns:
        ``external_id`` → ``company_id``, covering every posting. A posting
        whose employer could not be parsed at all is mapped to ``unmatched``
        alongside the ones that were parsed but did not match, because both are
        the same thing to the operator: a lead that needs a human to say who it
        is from.
    """
    grouped = alert_company_names(postings)
    all_ids = [posting.external_id for posting in postings]
    resolved: dict[str, int] = {}
    unmatched_id: int | None = None
    matched_names = 0

    for name, external_ids in grouped.items():
        match = await match_company(session, name, threshold=threshold)
        if match is None:
            continue
        matched_names += 1
        log.debug(
            "alert_company_matched",
            source_id=source_id,
            parsed_name=name,
            company_id=match.company_id,
            company_name=match.name,
            score=round(match.score, 3),
        )
        for external_id in external_ids:
            resolved[external_id] = match.company_id

    missing = [external_id for external_id in all_ids if external_id not in resolved]
    if missing:
        unmatched_id = await get_unmatched_company_id(session)
        for external_id in missing:
            resolved[external_id] = unmatched_id

    log.info(
        "alert_companies_resolved",
        source_id=source_id,
        postings=len(all_ids),
        distinct_names=len(grouped),
        matched_names=matched_names,
        unmatched_postings=len(missing),
    )
    return resolved


__all__ = [
    "ALERT_COMPANY_KEY",
    "ALERT_RAW_FLAGS",
    "NEEDS_DESCRIPTION_KEY",
    "UNMATCHED_NAME",
    "UNMATCHED_SLUG",
    "UNMATCHED_TAGS",
    "CompanyMatch",
    "alert_company_names",
    "get_unmatched_company_id",
    "match_company",
    "resolve_alert_companies",
]
