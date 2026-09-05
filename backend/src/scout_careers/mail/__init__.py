"""Gmail access: OAuth, the encrypted token store, and the mailbox reader.

The module boundary is one-directional and load-bearing. ``sources/`` declares
the :class:`~scout_careers.sources.base.MailReader` protocol it needs and
imports nothing from here; ``mail/`` imports ``sources/base.py`` for the DTO and
the protocol and imports nothing back. ``ingest/`` is the only place the two
meet, and it meets them by injection (SOURCE_ADAPTERS.md §7).

Phase 1 requests exactly one scope, ``gmail.readonly``. The digest's
``gmail.send`` belongs to Phase 2 and is not requested now: asking for less than
you will eventually need is the correct default, and a second consent screen is
the cheapest thing in this document.
"""

from __future__ import annotations

from scout_careers.mail.gmail import GmailClient, GmailTransport, build_mail_reader
from scout_careers.mail.oauth import authorise_gmail, client_secrets_guidance
from scout_careers.mail.scopes import GMAIL_READONLY_SCOPE, PHASE_1_SCOPES
from scout_careers.mail.tokens import OAuthToken, TokenStore

__all__ = [
    "GMAIL_READONLY_SCOPE",
    "PHASE_1_SCOPES",
    "GmailClient",
    "GmailTransport",
    "OAuthToken",
    "TokenStore",
    "authorise_gmail",
    "build_mail_reader",
    "client_secrets_guidance",
]
