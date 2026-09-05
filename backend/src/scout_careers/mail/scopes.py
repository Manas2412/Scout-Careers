"""The OAuth scopes this build requests. A code constant, deliberately.

``EMAIL_INGESTION.md`` §2.3 names two scopes for the finished system:
``gmail.readonly`` for reading, ``gmail.send`` for the daily digest. Phase 1
implements reading and nothing else, so it requests reading and nothing else.

Three properties of this module are the point of it existing at all:

- **The set is a frozen tuple, not a setting.** There is no environment variable
  that widens the grant. Widening it is a code change, a review and a re-consent.
- **``gmail.modify`` is absent and will stay absent.** It is a write scope on the
  operator's primary correspondence; read-only makes "the loop mutated my real
  mail" structurally impossible rather than merely unlikely (§2.4).
- **``gmail.send`` is absent in Phase 1.** Requesting it now would mean holding
  the capability to send as the operator for however long it takes Phase 2 to
  arrive, in exchange for saving one browser consent later.
"""

from __future__ import annotations

from typing import Final

#: Read message metadata and bodies. Restricted scope; grants no write of any
#: kind — not labels, not read state, not Trash.
GMAIL_READONLY_SCOPE: Final[str] = "https://www.googleapis.com/auth/gmail.readonly"

#: Phase 2's digest scope, named here so the absence is visible rather than
#: merely undocumented. It is NOT in :data:`PHASE_1_SCOPES`.
GMAIL_SEND_SCOPE: Final[str] = "https://www.googleapis.com/auth/gmail.send"

#: Exactly what ``scout-careers auth gmail`` asks for today.
PHASE_1_SCOPES: Final[tuple[str, ...]] = (GMAIL_READONLY_SCOPE,)

__all__ = ["GMAIL_READONLY_SCOPE", "GMAIL_SEND_SCOPE", "PHASE_1_SCOPES"]
