"""Alias to model ID, and nothing else.

One small module because the indirection it provides is what keeps a provider
swap a configuration change. Services ask for ``fast``; this decides what that
means today.
"""

from __future__ import annotations

from typing import Final

from scout_careers.common.config import Settings
from scout_careers.llm.base import Alias, LLMConfigError

#: Spellings that mean "whatever is newest". Refused, because a model that
#: changes without a deploy invalidates every eval result and every provenance
#: record (AI_ARCHITECTURE.md §4.1). ``Settings`` refuses these at boot as well;
#: this is the second check, at the point of use, for a model ID that reached us
#: some other way — a CLI override, an eval pin, a test.
UNPINNED_MARKERS: Final[tuple[str, ...]] = ("-latest", ":latest", "/latest")


class ModelRouter:
    """Resolves ``fast`` / ``strong`` to the configured provider model ID.

    Args:
        settings: Supplies the two pinned IDs.
    """

    def __init__(self, settings: Settings) -> None:
        self._by_alias: dict[str, str] = {
            "fast": settings.llm_model_fast,
            "strong": settings.llm_model_strong,
        }

    def resolve(self, alias: Alias | str) -> str:
        """Return the model ID for an alias.

        Args:
            alias: ``fast`` or ``strong``.

        Returns:
            The pinned provider model ID.

        Raises:
            LLMConfigError: On an unknown alias, or a model ID that names a
                floating version.
        """
        try:
            model_id = self._by_alias[alias]
        except KeyError:
            raise LLMConfigError(
                f"unknown model alias {alias!r}; known: {', '.join(sorted(self._by_alias))}"
            ) from None
        lowered = model_id.strip().lower()
        if any(marker in lowered for marker in UNPINNED_MARKERS):
            raise LLMConfigError(f"model IDs must be pinned; {model_id!r} names a floating version")
        return model_id

    @property
    def aliases(self) -> tuple[str, ...]:
        """The known aliases, for error messages and health output."""
        return tuple(sorted(self._by_alias))


__all__ = ["UNPINNED_MARKERS", "ModelRouter"]
