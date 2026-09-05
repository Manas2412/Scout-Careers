"""Externally addressable IDs.

ULIDs, stored as ``CHAR(26)``: they sort by creation time, they are safe in a
URL, and they do not leak a row count the way an identity column does
(DATA_MODEL.md §1).
"""

from __future__ import annotations

from ulid import ULID

ULID_LENGTH = 26


def new_ulid() -> str:
    """Return a fresh 26-character Crockford-base32 ULID.

    Returns:
        The canonical string form, suitable for a ``CHAR(26)`` primary key.
    """
    return str(ULID())


def is_ulid(value: str) -> bool:
    """Report whether ``value`` is a well-formed ULID string.

    Args:
        value: Candidate identifier, typically from a URL path.

    Returns:
        True when the value parses as a ULID.
    """
    if len(value) != ULID_LENGTH:
        return False
    try:
        ULID.from_str(value)
    except (ValueError, TypeError):
        return False
    return True


__all__ = ["ULID_LENGTH", "is_ulid", "new_ulid"]
