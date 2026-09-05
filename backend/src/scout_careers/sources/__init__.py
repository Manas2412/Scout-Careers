"""Source adapters.

This layer never touches the database. It fetches, normalises and yields DTOs;
``ingest/`` persists them. The import-linter contract in the pre-merge gate
enforces it.
"""

from __future__ import annotations
