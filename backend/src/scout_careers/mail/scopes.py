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
- **``gmail.send`` is requested only once the digest exists.** Phase 1 declined
  it deliberately: holding the capability to send as the operator for however
  long Phase 2 took, in exchange for saving one browser consent, was the wrong
  trade. Phase 2 builds the digest, so the trade is now the other way round and
  :data:`PHASE_2_SCOPES` asks for it.

``gmail.send`` is narrower than it sounds, and narrower than ``gmail.compose``:
it can **only** send. It cannot read, list, label, modify or delete, and it
cannot create drafts. So the read/write asymmetry that made read-only valuable
survives — nothing here can alter a message that already exists in the mailbox.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

#: Read message metadata and bodies. Restricted scope; grants no write of any
#: kind — not labels, not read state, not Trash.
GMAIL_READONLY_SCOPE: Final[str] = "https://www.googleapis.com/auth/gmail.readonly"

#: Send, and nothing else. Not ``gmail.compose`` — that one can also create and
#: manage drafts, which is write access to mailbox contents for a capability
#: this system does not need.
GMAIL_SEND_SCOPE: Final[str] = "https://www.googleapis.com/auth/gmail.send"

#: What Phase 1 asked for. Kept as a named constant rather than deleted: a
#: token stored before the digest existed carries exactly this set, and
#: :func:`scout_careers.mail.oauth.missing_scopes` compares against it to tell
#: an under-scoped token apart from an unauthorised install.
PHASE_1_SCOPES: Final[tuple[str, ...]] = (GMAIL_READONLY_SCOPE,)

#: Exactly what ``scout-careers auth gmail`` asks for today. Reading is still
#: required — the digest reports on alerts the reader ingested.
PHASE_2_SCOPES: Final[tuple[str, ...]] = (GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE)

#: The set the current build needs. Every caller uses this name, so the next
#: phase that changes scopes changes one line and every message stays true.
REQUIRED_SCOPES: Final[tuple[str, ...]] = PHASE_2_SCOPES


def missing_scopes(granted: Sequence[str]) -> tuple[str, ...]:
    """Which required scopes a stored token does not carry.

    Args:
        granted: The scopes on the stored credential.

    Returns:
        The missing ones, in :data:`REQUIRED_SCOPES` order; empty when the token
        is sufficient.

    A token predating the digest is *valid* and *insufficient*, and those are
    different faults with different fixes. Without this, adding the send scope
    turns every existing install's first digest into a 403 from deep inside the
    send path — at 08:15, on a schedule, where nobody is watching.
    """
    held = {scope.strip() for scope in granted}
    return tuple(scope for scope in REQUIRED_SCOPES if scope not in held)


__all__ = [
    "GMAIL_READONLY_SCOPE",
    "GMAIL_SEND_SCOPE",
    "PHASE_1_SCOPES",
    "PHASE_2_SCOPES",
    "REQUIRED_SCOPES",
    "missing_scopes",
]
