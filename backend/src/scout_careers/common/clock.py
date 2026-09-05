"""Time. Everything is stored and computed in UTC; IST is display only."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

#: Display timezone. Storage and computation are UTC regardless
#: (ARCHITECTURE.md §8).
IST = ZoneInfo("Asia/Kolkata")


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Returns:
        A ``datetime`` with ``tzinfo`` set to UTC. Never naive — a naive
        datetime is a timezone bug waiting for a deploy to a differently
        configured host, which is why ruff's ``DTZ`` rules are enabled.
    """
    return datetime.now(UTC)


def to_ist(moment: datetime) -> datetime:
    """Convert a timezone-aware datetime to Asia/Kolkata for display.

    Args:
        moment: A timezone-aware datetime.

    Returns:
        The same instant expressed in IST.

    Raises:
        ValueError: If ``moment`` is naive.
    """
    if moment.tzinfo is None:
        raise ValueError("to_ist requires a timezone-aware datetime")
    return moment.astimezone(IST)


__all__ = ["IST", "to_ist", "utcnow"]
