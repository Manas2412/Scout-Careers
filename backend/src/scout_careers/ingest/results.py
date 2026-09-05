"""Folding per-source outcomes into what ``run_log`` stores.

``SourceResult`` already exists in ``sources/base.py`` — it is what an adapter's
run produces, and it is deliberately declared next to the adapter contract so a
fixture-replay test can assert on it without importing ``ingest/``. What lives
here is the aggregation the runner needs and nothing else:

- :class:`RunStats`, the shape written to ``run_log.stats``;
- :func:`fold_results`, which folds a run's results into it;
- :func:`source_results_payload`, which renders ``run_log.source_results`` in
  the SOURCE_ADAPTERS.md §10.3 shape verbatim — singular ``error``, alongside
  ``error_code``;
- :func:`resolve_run_status`, §10.4's state machine.

The last one is where invariant 5 becomes a value rather than a promise: no
combination of adapter failures can make this function return ``failed``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scout_careers.common.types import RunStatus, SourceStatus
from scout_careers.sources.base import SourceResult

#: Statuses that mean the source completed its work, whether or not it had
#: anything to yield. Everything else is a failure for §10.4's purposes, except
#: ``disabled``, which was never attempted.
_COMPLETED_STATUSES: frozenset[SourceStatus] = frozenset(
    {SourceStatus.OK, SourceStatus.EMPTY, SourceStatus.DISABLED}
)


class RunStats(BaseModel):
    """The ``run_log.stats`` document for a discovery run.

    ``API.md`` §7 shows ``stats`` carrying ``filtered``, ``extracted``,
    ``scored``, ``generated``, ``validation_failures`` and ``llm_cost_inr``.
    Those are stages ④–⑨ and they are not in Phase 1 scope, so they are not
    written: a key that is always zero reads as "nothing was filtered", which is
    a different and wrong statement from "filtering does not run yet". They are
    added by the phase that earns them.
    """

    model_config = ConfigDict(extra="forbid")

    # -- per-source outcome counts ---------------------------------------
    sources_total: int = 0
    sources_ok: int = 0
    sources_empty: int = 0
    sources_failed: int = 0
    sources_disabled: int = 0

    # -- posting counts ---------------------------------------------------
    fetched: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    superseded: int = 0
    closed: int = 0

    # -- network behaviour -------------------------------------------------
    requests: int = 0
    retries: int = 0
    rate_limit_wait_ms: int = 0

    duration_ms: int = 0


def fold_results(
    results: Sequence[SourceResult],
    *,
    duration_ms: int = 0,
    superseded: int = 0,
    closed: int = 0,
) -> RunStats:
    """Fold every per-source result into one ``RunStats``.

    Args:
        results: One entry per attempted source, in any order.
        duration_ms: Wall-clock time of the whole run.
        superseded: Postings collapsed by :mod:`~scout_careers.ingest.dedup`.
        closed: Postings closed by :mod:`~scout_careers.ingest.close`.

    Returns:
        The aggregate, with ``skipped`` summed per reason across sources so a
        board that dropped forty descriptions is visible without opening a
        single source result.
    """
    stats = RunStats(duration_ms=duration_ms, superseded=superseded, closed=closed)
    skipped: dict[str, int] = {}

    for result in results:
        stats.sources_total += 1
        if result.status is SourceStatus.OK:
            stats.sources_ok += 1
        elif result.status is SourceStatus.EMPTY:
            stats.sources_empty += 1
        elif result.status is SourceStatus.DISABLED:
            stats.sources_disabled += 1
        else:
            stats.sources_failed += 1

        stats.fetched += result.fetched
        stats.new += result.new
        stats.updated += result.updated
        stats.unchanged += result.unchanged
        stats.requests += result.requests
        stats.retries += result.retries
        stats.rate_limit_wait_ms += result.rate_limit_wait_ms
        for reason, count in result.skipped.items():
            skipped[reason] = skipped.get(reason, 0) + count

    stats.skipped = dict(sorted(skipped.items()))
    return stats


def source_results_payload(results: Iterable[SourceResult]) -> list[dict[str, Any]]:
    """Render ``run_log.source_results``.

    Args:
        results: The run's per-source results.

    Returns:
        A JSON-safe list in the §10.3 shape: every field of ``SourceResult``,
        with the enums as their string values, ordered by ``source_id`` so two
        runs of the same registry produce comparable documents.
    """
    ordered = sorted(results, key=lambda result: result.source_id)
    return [result.model_dump(mode="json") for result in ordered]


def resolve_run_status(results: Iterable[SourceResult]) -> RunStatus:
    """Resolve ``run_log.status`` from the per-source outcomes (§10.4).

    Args:
        results: The run's per-source results.

    Returns:
        ``completed`` when every source ended ``ok``, ``empty`` or ``disabled``;
        ``completed_with_errors`` otherwise.

        Never ``failed``. ``failed`` means the run could not proceed — Postgres
        or Redis unavailable, the lock unheld, the runner itself raising — and
        the runner sets it directly. Adapter failures never produce it, which is
        invariant 5 expressed as a state machine.
    """
    if all(result.status in _COMPLETED_STATUSES for result in results):
        return RunStatus.COMPLETED
    return RunStatus.COMPLETED_WITH_ERRORS


def failure_summary(results: Iterable[SourceResult]) -> str | None:
    """Return a one-line, non-sensitive summary of a run's failures.

    Args:
        results: The run's per-source results.

    Returns:
        ``None`` when nothing failed, otherwise a count-per-status line for
        ``run_log.error``. Individual curated messages stay in
        ``source_results``; this is the digest headline.
    """
    counts: dict[str, int] = {}
    for result in results:
        if result.status in _COMPLETED_STATUSES:
            continue
        counts[result.status.value] = counts.get(result.status.value, 0) + 1
    if not counts:
        return None
    parts = [f"{count} {status}" for status, count in sorted(counts.items())]
    return f"{sum(counts.values())} source(s) failed: {', '.join(parts)}"


__all__ = [
    "RunStats",
    "failure_summary",
    "fold_results",
    "resolve_run_status",
    "source_results_payload",
]
