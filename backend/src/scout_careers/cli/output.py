"""Terminal rendering: plain tables, plain JSON, no framework.

``typer.echo`` rather than ``print``: ``print`` is banned in ``src/`` by the
pre-merge gate, because a stray debugging print in a scheduled run writes to
stdout where structured logs are supposed to be.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import typer

#: Two spaces between columns. Enough to read, narrow enough that a source
#: results table fits an 80-column terminal.
COLUMN_GAP = "  "


def echo(line: str = "") -> None:
    """Write one line to stdout."""
    typer.echo(line)


def error(line: str) -> None:
    """Write one line to stderr."""
    typer.echo(line, err=True)


def echo_json(payload: Any) -> None:
    """Write a payload as indented, key-sorted JSON.

    Args:
        payload: Anything ``json.dumps`` can render; datetimes fall back to
            ``str``.
    """
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a fixed-width table.

    Args:
        headers: Column headings.
        rows: Row values; every cell is stringified by the caller.

    Returns:
        The table, or a single "none" line when there are no rows. An empty
        table with headings reads as an error; "none" reads as an answer.
    """
    if not rows:
        return "  (none)"
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    lines = [
        COLUMN_GAP.join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        COLUMN_GAP.join("-" * width for width in widths),
    ]
    lines.extend(
        COLUMN_GAP.join(cell.ljust(widths[index]) for index, cell in enumerate(row)) for row in rows
    )
    return "\n".join(lines)


def echo_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Render and write a table."""
    typer.echo(render_table(headers, rows))


def dash(value: object) -> str:
    """Render a value for a table cell, with a dash for nothing.

    Args:
        value: Any value.

    Returns:
        The string form, or ``"—"`` when it is ``None`` or empty.
    """
    if value is None:
        return "—"
    text = str(value)
    return text if text else "—"


__all__ = ["COLUMN_GAP", "dash", "echo", "echo_json", "echo_table", "error", "render_table"]
