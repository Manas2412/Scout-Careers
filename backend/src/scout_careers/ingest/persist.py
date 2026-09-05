"""Stage ②: ``RawPosting`` → ``job_posting`` rows.

What this module owns, and why each belongs here rather than in an adapter:

- **The content hash.** ``content_hash(description_text)`` is computed here,
  once, from the text that is actually stored. An adapter that computed its own
  could change change-detection semantics for one board by accident, which is
  precisely the failure ``common/hashing.py`` exists to prevent
  (SOURCE_ADAPTERS.md §2.2).
- **Identity.** ``(source_id, external_id)`` (DATA_MODEL.md §4.1), expressed as
  ``INSERT ... ON CONFLICT DO UPDATE`` so that re-running a source is a no-op
  rather than a duplicate-key error. A discovery run is retried by hand often
  enough that idempotence is a feature, not a nicety.
- **Change detection.** Same identity, different hash ⇒ the row is rewritten and
  counted ``updated``. Same hash ⇒ only ``last_seen_at`` moves and it is counted
  ``unchanged``. A new identity is ``new``. The three counts are what the digest
  reports and what the operator reads to decide whether a board is alive.
- **The §9.1 description bound**, re-applied at the boundary. The adapters
  already apply it; doing it again here is deliberate defence in depth, because
  this is the last point before untrusted text reaches a column that later feeds
  a prompt.

Every posting seen in a run also gets ``missed_runs = 0`` and ``closed_at =
NULL``: being seen is the only evidence that closes the two-run rule's question,
and it is available here and nowhere else.

The plan/apply split is not ceremony. :func:`plan_persist` is pure, so the
classification rules have offline tests with no database in sight;
:func:`persist_postings` is the thin half that issues two statements.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scout_careers.common.hashing import content_hash
from scout_careers.common.ids import new_ulid
from scout_careers.common.logging import get_logger
from scout_careers.common.text import truncate_at_paragraph
from scout_careers.db.models import JobPosting
from scout_careers.sources.base import RawPosting

log = get_logger(__name__)

#: Postgres caps a statement at 65535 bind parameters. A ``job_posting`` row
#: carries ~20 of them, so 500 rows per statement leaves a wide margin and still
#: sends a 3,000-posting board in six round trips.
UPSERT_CHUNK = 500

#: The same margin for the identity lookup, which binds one parameter per id.
LOOKUP_CHUNK = 1_000

#: Recorded in ``job_posting.raw`` when the §9.1 bound actually cut something.
#: The key matches what the adapters already write, so the posting inspector has
#: one flag to read rather than two.
TRUNCATION_FLAG = "description_truncated"
TRUNCATION_ORIGINAL_CHARS = "description_original_chars"


class Change(StrEnum):
    """How one fetched posting compares to what is already stored."""

    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class ExistingPosting:
    """The stored state a fetched posting is compared against.

    Attributes:
        id: The stored ULID, reused rather than reallocated on an update.
        content_hash: The stored hash, which is the whole of change detection
            for the text.
        company_id: Which company the row is currently filed under. Carried so
            that a mail-alert lead sitting on the reserved ``unmatched`` row
            re-attaches the moment its employer is added to the registry —
            without it, promoting a company would leave every lead already
            discovered for it stranded, and the operator would have to notice
            that and fix it by hand.
    """

    id: str
    content_hash: str
    company_id: int


@dataclass(frozen=True, slots=True)
class PersistCounts:
    """The three change-detection counts for one source."""

    new: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        """Return the number of postings classified."""
        return self.new + self.updated + self.unchanged


@dataclass(slots=True)
class PersistPlan:
    """What :func:`persist_postings` will execute.

    Attributes:
        upserts: Column dictionaries for postings that are new or changed.
        touched_ids: Ids of postings whose content is unchanged; they need
            ``last_seen_at`` moved and nothing else.
        counts: The classification result.
    """

    upserts: list[dict[str, Any]] = field(default_factory=list)
    touched_ids: list[str] = field(default_factory=list)
    counts: PersistCounts = field(default_factory=PersistCounts)


def bound_description(text: str, limit: int) -> tuple[str, bool]:
    """Apply the §9.1 description bound at the persistence boundary.

    Args:
        text: The adapter's normalised description text.
        limit: ``settings.max_description_chars``.

    Returns:
        The bounded text and whether it was cut. The cut lands on a paragraph
        boundary and leaves a marker, so a truncated description reads as
        truncated rather than as a description that stops mid-sentence.
    """
    bounded = truncate_at_paragraph(text, limit)
    return bounded, len(bounded) != len(text)


def classify(
    existing: ExistingPosting | None,
    fetched_hash: str,
    *,
    company_id: int | None = None,
) -> Change:
    """Classify one fetched posting against its stored counterpart.

    Args:
        existing: The stored row for this ``(source_id, external_id)``, if any.
        fetched_hash: The hash of the text just fetched.
        company_id: Where this posting should now be filed. When it differs from
            where the row is filed, the posting counts as ``UPDATED`` even
            though its text is byte-identical: an unchanged posting takes the
            cheap ``last_seen_at`` path, which rewrites no columns, so a
            re-attachment expressed any other way would be silently dropped.
            Omitted means "not being moved" — the case for every board adapter,
            whose source is an employer and cannot change company.

    Returns:
        ``NEW`` when the identity is unknown, ``UPDATED`` when the hash or the
        company moved, ``UNCHANGED`` when neither did.
    """
    if existing is None:
        return Change.NEW
    if existing.content_hash != fetched_hash:
        return Change.UPDATED
    if company_id is not None and existing.company_id != company_id:
        return Change.UPDATED
    return Change.UNCHANGED


def plan_persist(
    postings: Iterable[RawPosting],
    *,
    source_id: int,
    company_id: int,
    existing: Mapping[str, ExistingPosting],
    now: datetime,
    max_description_chars: int,
    company_overrides: Mapping[str, int] | None = None,
    raw_extra: Mapping[str, Any] | None = None,
    id_factory: Callable[[], str] = new_ulid,
) -> PersistPlan:
    """Classify a source's postings and build the rows to write.

    Pure: no session, no clock, no id generation that the caller cannot control.
    That is what lets the classification rules — the ones the digest's "6 new, 3
    updated" line depends on — be tested offline.

    Args:
        postings: The adapter's output for this source, already normalised.
        source_id: The source these postings belong to.
        company_id: Its company, denormalised onto every posting so the dedup
            key and the Companies page do not need a join. This is the right
            answer for every board adapter, where the source *is* an employer.
        existing: Stored ``(external_id → ExistingPosting)`` for this source.
        now: The run's clock for this source. One value for the whole source, so
            ``last_seen_at`` is comparable across its postings and the close rule
            can key on it.
        max_description_chars: ``settings.max_description_chars``.
        company_overrides: ``external_id → company_id`` for the one adapter
            whose source is not an employer. A ``mail_alert`` digest carries
            roles at many companies, and :mod:`~scout_careers.ingest.resolve`
            has already decided which; a posting absent from this mapping keeps
            ``company_id``. Resolution is not done here because it needs the
            database and this function is pure.
        raw_extra: Keys merged into every row's ``raw``, after the adapter's own
            and before the truncation flags. Used to mark mail-sourced rows
            ``needs_description``.
        id_factory: ULID generator; injectable for deterministic tests.

    Returns:
        The plan, with each posting in exactly one bucket. A duplicate
        ``external_id`` inside one fetch is dropped with a WARN rather than sent
        to Postgres, where ``ON CONFLICT`` would refuse the whole statement for
        touching the same row twice.
    """
    plan = PersistPlan()
    new = updated = unchanged = 0
    seen_in_fetch: set[str] = set()

    for posting in postings:
        if posting.external_id in seen_in_fetch:
            log.warning(
                "posting_duplicate_external_id",
                source_id=source_id,
                external_id=posting.external_id,
            )
            continue
        seen_in_fetch.add(posting.external_id)

        description_text, truncated = bound_description(
            posting.description_text, max_description_chars
        )
        fetched_hash = content_hash(description_text)
        stored = existing.get(posting.external_id)
        row_company_id = (
            company_overrides.get(posting.external_id, company_id)
            if company_overrides is not None
            else company_id
        )
        change = classify(stored, fetched_hash, company_id=row_company_id)

        if change is Change.UNCHANGED and stored is not None:
            unchanged += 1
            plan.touched_ids.append(stored.id)
            continue

        if change is Change.NEW:
            new += 1
        else:
            updated += 1

        plan.upserts.append(
            _row(
                posting,
                source_id=source_id,
                company_id=row_company_id,
                posting_id=stored.id if stored is not None else id_factory(),
                description_text=description_text,
                truncated=truncated,
                fetched_hash=fetched_hash,
                raw_extra=raw_extra,
                now=now,
            )
        )

    plan.counts = PersistCounts(new=new, updated=updated, unchanged=unchanged)
    return plan


def _row(
    posting: RawPosting,
    *,
    source_id: int,
    company_id: int,
    posting_id: str,
    description_text: str,
    truncated: bool,
    fetched_hash: str,
    now: datetime,
    raw_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one ``job_posting`` column dictionary."""
    # The adapter's ``raw`` is copied, never mutated: ``RawPosting`` is frozen
    # so that ``ingest/`` cannot edit an adapter's output in place, and a dict
    # field is the one hole in that guarantee.
    raw: dict[str, Any] = dict(posting.raw)
    if raw_extra:
        raw.update(raw_extra)
    if truncated:
        raw[TRUNCATION_FLAG] = True
        raw[TRUNCATION_ORIGINAL_CHARS] = len(posting.description_text)

    return {
        "id": posting_id,
        "company_id": company_id,
        "source_id": source_id,
        "external_id": posting.external_id,
        "title": posting.title,
        "department": posting.department,
        "location_raw": posting.location_raw,
        "location_city": posting.location_city,
        "location_country": posting.location_country,
        "is_remote": posting.is_remote,
        "employment_type": posting.employment_type,
        "seniority_guess": posting.seniority_guess,
        "url": str(posting.url),
        "description_html": posting.description_html,
        "description_text": description_text,
        "content_hash": fetched_hash,
        "posted_at": posting.posted_at,
        "first_seen_at": now,
        "last_seen_at": now,
        "closed_at": None,
        "missed_runs": 0,
        "raw": raw,
    }


#: Columns rewritten when a posting we already hold comes back changed.
#:
#: ``first_seen_at`` is absent on purpose — it records when *we* first saw the
#: role and recency ranking keys on it, so a recruiter's edit must not reset it.
#: ``filtered_out`` and ``filter_reason`` are absent because they belong to
#: ``ingest/dedup.py``: persisting a posting must not silently un-supersede it.
#:
#: ``company_id`` is present, and only ever moves for ``mail_alert``: for a board
#: adapter the value is ``source.company_id`` on every run and rewriting it is a
#: no-op. For an alert lead it is how a posting parked on the reserved
#: ``unmatched`` row reaches its real employer once that employer is tracked.
UPDATE_COLUMNS = (
    "company_id",
    "title",
    "department",
    "location_raw",
    "location_city",
    "location_country",
    "is_remote",
    "employment_type",
    "seniority_guess",
    "url",
    "description_html",
    "description_text",
    "content_hash",
    "posted_at",
    "last_seen_at",
    "closed_at",
    "missed_runs",
    "raw",
)


async def load_existing(
    session: AsyncSession,
    *,
    source_id: int,
    external_ids: Sequence[str],
) -> dict[str, ExistingPosting]:
    """Load the stored identity and hash for a source's fetched postings.

    Args:
        session: The session this source's work runs in.
        source_id: The source being persisted.
        external_ids: Every ``external_id`` the adapter yielded.

    Returns:
        A mapping from ``external_id`` to its stored id and hash. Missing keys
        are new postings.
    """
    found: dict[str, ExistingPosting] = {}
    for start in range(0, len(external_ids), LOOKUP_CHUNK):
        chunk = external_ids[start : start + LOOKUP_CHUNK]
        rows = await session.execute(
            select(
                JobPosting.external_id,
                JobPosting.id,
                JobPosting.content_hash,
                JobPosting.company_id,
            ).where(
                JobPosting.source_id == source_id,
                JobPosting.external_id.in_(chunk),
            )
        )
        for external_id, posting_id, stored_hash, stored_company_id in rows.all():
            found[external_id] = ExistingPosting(
                id=posting_id,
                content_hash=stored_hash,
                company_id=stored_company_id,
            )
    return found


async def apply_plan(session: AsyncSession, plan: PersistPlan, *, now: datetime) -> None:
    """Execute a persistence plan against Postgres.

    Args:
        session: The session this source's work runs in. The caller owns the
            transaction, and gives each source its own, so one board's failure
            cannot roll back another board's rows.
        plan: The plan from :func:`plan_persist`.
        now: The same clock value the plan was built with.
    """
    for start in range(0, len(plan.upserts), UPSERT_CHUNK):
        chunk = plan.upserts[start : start + UPSERT_CHUNK]
        stmt = insert(JobPosting).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[JobPosting.source_id, JobPosting.external_id],
                set_={column: getattr(stmt.excluded, column) for column in UPDATE_COLUMNS},
            )
        )

    for start in range(0, len(plan.touched_ids), LOOKUP_CHUNK):
        chunk_ids = plan.touched_ids[start : start + LOOKUP_CHUNK]
        # Unchanged postings move ``last_seen_at`` and nothing else about their
        # content. ``updated_at`` is not set here; the ``trg_job_posting_touch_
        # updated_at`` trigger owns that column, and a second writer would be a
        # second source of truth.
        await session.execute(
            update(JobPosting)
            .where(JobPosting.id.in_(chunk_ids))
            .values(last_seen_at=now, missed_runs=0, closed_at=None)
        )


async def persist_postings(
    session: AsyncSession,
    postings: Sequence[RawPosting],
    *,
    source_id: int,
    company_id: int,
    now: datetime,
    max_description_chars: int,
    company_overrides: Mapping[str, int] | None = None,
    raw_extra: Mapping[str, Any] | None = None,
) -> PersistCounts:
    """Persist one source's postings and report the change counts.

    Args:
        session: The session this source's work runs in.
        postings: The complete, buffered output of one source. Partial output is
            never persisted — a half-fetched board looks like "everything else
            closed" to the two-run rule (SOURCE_ADAPTERS.md §4.4).
        source_id: The source being persisted.
        company_id: Its company — correct for every board adapter.
        now: The clock value for this source's rows.
        max_description_chars: ``settings.max_description_chars``.
        company_overrides: Per-posting company, for ``mail_alert`` only. See
            :mod:`~scout_careers.ingest.resolve`.
        raw_extra: Keys merged into every row's ``raw``.

    Returns:
        The ``new`` / ``updated`` / ``unchanged`` counts for the source result.
    """
    if not postings:
        return PersistCounts()

    external_ids = [posting.external_id for posting in postings]
    existing = await load_existing(session, source_id=source_id, external_ids=external_ids)
    plan = plan_persist(
        postings,
        source_id=source_id,
        company_id=company_id,
        existing=existing,
        now=now,
        max_description_chars=max_description_chars,
        company_overrides=company_overrides,
        raw_extra=raw_extra,
    )
    await apply_plan(session, plan, now=now)
    return plan.counts


__all__ = [
    "LOOKUP_CHUNK",
    "TRUNCATION_FLAG",
    "TRUNCATION_ORIGINAL_CHARS",
    "UPDATE_COLUMNS",
    "UPSERT_CHUNK",
    "Change",
    "ExistingPosting",
    "PersistCounts",
    "PersistPlan",
    "apply_plan",
    "bound_description",
    "classify",
    "load_existing",
    "persist_postings",
    "plan_persist",
]
