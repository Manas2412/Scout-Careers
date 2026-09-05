"""Content hashing. One algorithm, one place.

``content_hash`` lives here rather than on ``RawPosting`` so that no adapter can
change change-detection semantics by accident (SOURCE_ADAPTERS.md §2.2).
"""

from __future__ import annotations

import hashlib

from scout_careers.common.text import normalise_for_hash


def content_hash(text: str) -> str:
    """Return the sha256 hexdigest of the normalised text.

    Args:
        text: Description text, in any cosmetic form.

    Returns:
        A 64-character lowercase hex digest, stable across HTML wrapping,
        Unicode compatibility forms and whitespace differences.
    """
    return hashlib.sha256(normalise_for_hash(text).encode("utf-8")).hexdigest()


__all__ = ["content_hash"]
