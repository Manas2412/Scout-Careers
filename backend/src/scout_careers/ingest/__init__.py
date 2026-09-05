"""Ingestion: the only layer that persists.

``sources/`` fetches and normalises; ``ingest/`` decides what a ``RawPosting``
means for the database. The split is enforced by the import-linter contract in
the pre-merge gate — ``sources/`` may not import ``db/`` — and it is what makes
adapter tests replayable against fixtures with no database in sight.

Phase 1 runs pipeline stages ① DISCOVER, ② NORMALISE and ③ DEDUPE
(``ARCHITECTURE.md`` §6). Stage ④ FILTER and everything downstream arrive with
their own phases; nothing here filters, scores or spends a token.

The modules, in the order a run touches them:

- :mod:`~scout_careers.ingest.runner` — stage ①, and the whole of invariant 5.
- :mod:`~scout_careers.ingest.persist` — stages ② and ③'s per-source half:
  ``RawPosting`` → ``job_posting`` rows, with change detection.
- :mod:`~scout_careers.ingest.dedup` — stage ③'s cross-source half, run once
  per run after every source has been persisted.
- :mod:`~scout_careers.ingest.close` — the two-run close rule, scoped per source.
- :mod:`~scout_careers.ingest.results` — folding per-source outcomes into
  ``run_log.stats`` and ``run_log.source_results``.
"""

from __future__ import annotations
