"""Text normalisation shared by hashing, dedup and the adapters."""

from __future__ import annotations

import re
import unicodedata

#: Characters that carry no meaning but change a hash. Recruiters paste from
#: Word, Notion and Google Docs; each leaves a different set behind.
_ZERO_WIDTH: dict[int, None] = dict.fromkeys(
    [
        0x200B,  # zero-width space
        0x200C,  # zero-width non-joiner
        0x200D,  # zero-width joiner
        0xFEFF,  # zero-width no-break space / BOM
    ]
)

_WHITESPACE_RUN = re.compile(r"\s+")


def normalise_for_hash(value: str) -> str:
    """Canonicalise text so that cosmetic edits do not look like content edits.

    NFKC-normalises, drops zero-width characters, collapses every run of
    whitespace (including newlines) to a single space, and strips the ends.

    Args:
        value: Raw text, typically a job description.

    Returns:
        The canonical form used as the input to :func:`~.hashing.content_hash`.
    """
    normalised = unicodedata.normalize("NFKC", value)
    normalised = normalised.translate(_ZERO_WIDTH)
    normalised = _WHITESPACE_RUN.sub(" ", normalised)
    return normalised.strip()


def truncate(value: str | None, limit: int, *, marker: str = "…") -> str | None:
    """Truncate to ``limit`` characters, marker included.

    Args:
        value: Text to bound, or ``None``.
        limit: Maximum length of the result, including ``marker``.
        marker: Appended when truncation happened.

    Returns:
        ``None`` if ``value`` was ``None``, otherwise text no longer than
        ``limit``.
    """
    if value is None:
        return None
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit <= len(marker):
        return value[:limit]
    return value[: limit - len(marker)] + marker


def truncate_at_paragraph(value: str, limit: int, *, marker: str = "\n\n[truncated]") -> str:
    """Truncate at the last paragraph boundary at or before ``limit``.

    Used for ``description_text``: an unbounded JD is a token-cost hazard and a
    prompt-injection surface (SOURCE_ADAPTERS.md §9.1). Cutting on a paragraph
    keeps the remaining text readable to the extractor.

    Args:
        value: The description text.
        limit: Maximum length of the result, including ``marker``.
        marker: Appended when truncation happened.

    Returns:
        Text no longer than ``limit``.
    """
    if len(value) <= limit:
        return value
    budget = max(0, limit - len(marker))
    head = value[:budget]
    boundary = head.rfind("\n\n")
    if boundary > budget // 2:
        head = head[:boundary]
    return head.rstrip() + marker


__all__ = ["normalise_for_hash", "truncate", "truncate_at_paragraph"]
