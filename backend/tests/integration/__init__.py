"""Tests that need a real Postgres.

Skipped unless ``DATABASE_URL`` is set. The pre-merge gate runs ``tests/unit``
only, so these can never become a check that passes because it quietly did not
run — a green tick for a suite that skipped itself is worse than no tick.
"""

from __future__ import annotations
