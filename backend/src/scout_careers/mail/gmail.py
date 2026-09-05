"""``GmailClient`` — the concrete :class:`~scout_careers.sources.base.MailReader`.

This is the only thing in Phase 1 that touches the operator's mailbox, and it
does exactly one thing: hand the ``mail_alert`` adapter the alert messages that
have already arrived. It does not label, mark read, archive, move or delete —
Phase 1 holds ``gmail.readonly`` and nothing else, which makes that whole class
of bug structurally impossible rather than merely absent (EMAIL_INGESTION.md
§2.4).

**Bodies are never persisted.** They are decoded into a
:class:`~scout_careers.sources.base.MailMessage`, handed to the parser, and
dropped when the run ends. ``DATA_MODEL.md`` is explicit that only a class, a
confidence and short excerpts are ever stored, and none of those exist yet. They
are also never logged, at any level, along with the subject line and the full
sender address — the log carries the message id and the sender's *domain*
(SECURITY_ARCHITECTURE.md §7.3).

**The transport is a seam, not an abstraction for its own sake.**
:class:`GmailTransport` is the two Gmail calls this needs, expressed as a
protocol; :class:`~scout_careers.mail.transport.GoogleGmailTransport` implements
it over ``google-api-python-client``. Everything interesting — the query, the
sender allow-list, the lookback bound, MIME walking, the never-log rules — lives
here, above the seam, and is therefore tested with no Google library and no
socket in the room.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any, Final, Protocol, runtime_checkable

from redis.asyncio import Redis

from scout_careers.common.config import Settings
from scout_careers.common.errors import MailNotConfigured
from scout_careers.common.logging import get_logger
from scout_careers.sources.base import MailMessage, MailReader

log = get_logger(__name__)

#: The Gmail search operator that bounds a read by receipt time. Seconds since
#: the epoch, which is the only form that does not depend on the mailbox's
#: display timezone — ``newer_than:2d`` would quietly mean something different
#: for an account set to a different zone.
_AFTER_OPERATOR: Final[str] = "after"

#: Body MIME types, in the order the parsers want them. HTML first: every alert
#: template is HTML, and the plaintext alternative is usually a stub.
_BODY_PREFERENCE: Final[tuple[str, ...]] = ("text/html", "text/plain")

#: How deep a MIME tree is walked. A job alert is two or three levels; anything
#: deeper is malformed, and an unbounded walk over untrusted mail is a way to
#: spend a run's budget on one message.
_MAX_MIME_DEPTH: Final[int] = 8

#: Hard ceiling on one decoded body. Alert digests are tens of kilobytes.
MAX_BODY_BYTES: Final[int] = 2 * 1024 * 1024


@runtime_checkable
class GmailTransport(Protocol):
    """The two Gmail API calls this module makes, and no others.

    Narrow on purpose. A transport that could also modify, send or delete would
    be a capability held by every caller of :class:`GmailClient`, and the scope
    ceiling would be the only thing standing between a bug and the operator's
    mail.
    """

    async def list_messages(self, *, query: str, max_results: int) -> Mapping[str, Any]:
        """Return the raw ``users.messages.list`` response for a Gmail query."""

    async def get_message(self, message_id: str) -> Mapping[str, Any]:
        """Return the raw ``users.messages.get`` resource, ``format=full``."""

    async def aclose(self) -> None:
        """Release whatever the transport owns."""


class GmailClient:
    """Reads job-alert mail. Satisfies ``MailReader`` structurally.

    Args:
        transport: The Gmail seam.
        max_messages: Ceiling on messages read per call.
    """

    def __init__(self, *, transport: GmailTransport, max_messages: int = 100) -> None:
        self._transport = transport
        self._max_messages = max_messages

    # -- MailReader --------------------------------------------------------

    async def list_recent_messages(
        self,
        *,
        label: str,
        senders: Sequence[str],
        since: datetime,
    ) -> Sequence[MailMessage]:
        """Return messages in ``label`` from ``senders`` received at or after ``since``.

        The allow-list and the window are applied **twice**: once in the Gmail
        query, so the server does the work, and again over what comes back. The
        second pass is not redundant. Gmail's ``from:`` operator matches on more
        than the envelope address — a display name containing the string
        satisfies it — and ``after:`` has day-level granularity in some
        accounts. The client-side pass is what makes "only these senders, only
        this window" true rather than approximately true, and it is the pass the
        tests assert on.

        Args:
            label: The Gmail label alerts are filed under.
            senders: The permitted envelope addresses. An empty list reads
                nothing: an empty allow-list is an allow-list, not a wildcard.
            since: The earliest receipt time to return, tz-aware.

        Returns:
            The matching messages, newest first, capped at ``max_messages``.

        Raises:
            ValueError: When ``since`` is naive. A naive bound would silently
                mean a different window on a differently configured host.
        """
        if since.tzinfo is None:
            raise ValueError("list_recent_messages requires a timezone-aware `since`")

        allowed = {address.strip().lower() for address in senders if address.strip()}
        if not allowed:
            log.info("gmail_read_skipped", label=label, reason="empty_sender_allow_list")
            return []

        query = build_query(label=label, senders=sorted(allowed), since=since)
        listing = await self._transport.list_messages(query=query, max_results=self._max_messages)
        ids = _message_ids(listing)[: self._max_messages]

        messages: list[MailMessage] = []
        rejected = 0
        for message_id in ids:
            resource = await self._transport.get_message(message_id)
            parsed = parse_message(resource)
            if parsed is None:
                rejected += 1
                continue
            if parsed.sender.lower() not in allowed or parsed.received_at < since:
                rejected += 1
                continue
            messages.append(parsed)

        messages.sort(key=lambda message: message.received_at, reverse=True)
        # Counts and a label. Never a subject, never a body, never a full
        # sender address (SECURITY_ARCHITECTURE.md §7.3).
        log.info(
            "gmail_messages_read",
            label=label,
            listed=len(ids),
            kept=len(messages),
            rejected=rejected,
            senders=len(allowed),
        )
        return messages

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Release the transport. Safe to call twice."""
        await self._transport.aclose()


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


def build_query(*, label: str, senders: Sequence[str], since: datetime) -> str:
    """Build the Gmail search query for one read.

    Args:
        label: The label to search. Quoted, because ``job alerts`` with a space
            is a perfectly ordinary label and an unquoted one silently becomes
            two terms.
        senders: The allow-list, already normalised.
        since: The lower bound on receipt time.

    Returns:
        A Gmail query string.
    """
    terms = [f'label:"{label}"']
    if senders:
        joined = " OR ".join(senders)
        terms.append(f"from:({joined})")
    terms.append(f"{_AFTER_OPERATOR}:{int(since.timestamp())}")
    return " ".join(terms)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _message_ids(listing: Mapping[str, Any]) -> list[str]:
    """Pull the message ids out of a ``users.messages.list`` response.

    Args:
        listing: The raw response.

    Returns:
        The ids, in the order Gmail returned them. A response with no
        ``messages`` key means an empty mailbox, which is not an error.
    """
    entries = listing.get("messages") or []
    if not isinstance(entries, list):
        return []
    ids: list[str] = []
    for entry in entries:
        if isinstance(entry, Mapping):
            identifier = entry.get("id")
            if isinstance(identifier, str) and identifier:
                ids.append(identifier)
    return ids


def parse_message(resource: Mapping[str, Any]) -> MailMessage | None:
    """Turn a ``users.messages.get`` resource into a ``MailMessage``.

    Args:
        resource: The raw message resource, ``format=full``.

    Returns:
        The message, or ``None`` when it has no id, no resolvable sender or no
        receipt time. A message that cannot be attributed is dropped rather
        than guessed at: the sender is what selects the parser, and the wrong
        parser produces plausible, wrong postings.
    """
    message_id = resource.get("id")
    if not isinstance(message_id, str) or not message_id:
        return None

    payload = resource.get("payload")
    headers = _headers(payload if isinstance(payload, Mapping) else {})
    sender = parseaddr(headers.get("from", ""))[1].strip()
    if not sender:
        return None

    received_at = _received_at(resource)
    if received_at is None:
        return None

    html_body, text_body = _bodies(payload if isinstance(payload, Mapping) else {})
    return MailMessage(
        message_id=message_id,
        sender=sender,
        subject=headers.get("subject", ""),
        received_at=received_at,
        html_body=html_body,
        text_body=text_body,
    )


def _headers(payload: Mapping[str, Any]) -> dict[str, str]:
    """Return the payload's headers, lower-cased by name.

    Args:
        payload: The message payload.

    Returns:
        A name → value map. Duplicate names keep the first occurrence, which is
        what a mail user agent shows.
    """
    result: dict[str, str] = {}
    entries = payload.get("headers") or []
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        value = entry.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result.setdefault(name.lower(), value)
    return result


def _received_at(resource: Mapping[str, Any]) -> datetime | None:
    """Resolve the message's receipt time.

    ``internalDate`` is milliseconds since the epoch and is the time Gmail
    *received* the message — not the ``Date:`` header, which is written by the
    sender and is therefore both untrusted and frequently wrong.

    Args:
        resource: The raw message resource.

    Returns:
        A tz-aware UTC datetime, or ``None`` when the field is absent or
        unparseable.
    """
    raw = resource.get("internalDate")
    try:
        millis = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def _bodies(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Walk the MIME tree and decode the HTML and plaintext parts.

    Args:
        payload: The message payload.

    Returns:
        ``(html, text)``, either of which may be ``None``. Attachments are
        ignored entirely: an alert's payload is its HTML, and fetching an
        attachment would be a second request for something no parser reads.
    """
    found: dict[str, str] = {}
    _walk(payload, found, depth=0)
    return found.get("text/html"), found.get("text/plain")


def _walk(part: Mapping[str, Any], found: dict[str, str], *, depth: int) -> None:
    """Depth-first walk of one MIME node, filling ``found`` with the first hit per type."""
    if depth > _MAX_MIME_DEPTH:
        return
    mime_type = str(part.get("mimeType") or "").lower()
    if mime_type in _BODY_PREFERENCE and mime_type not in found:
        decoded = _decode_body(part.get("body"))
        if decoded is not None:
            found[mime_type] = decoded
    children = part.get("parts") or []
    if not isinstance(children, list):
        return
    for child in children:
        if isinstance(child, Mapping):
            _walk(child, found, depth=depth + 1)


def _decode_body(body: Any) -> str | None:
    """Decode a ``body.data`` field.

    Args:
        body: The part's ``body`` object.

    Returns:
        The decoded text, or ``None`` when there is no inline data, the data is
        not valid base64url, or it exceeds :data:`MAX_BODY_BYTES`. A part with
        an ``attachmentId`` and no ``data`` is skipped without a second request.

        Decoding is ``errors="replace"``: third-party marketing mail declares
        charsets it does not honour, and a mojibake character in a snippet is a
        far better outcome than dropping a whole digest.
    """
    if not isinstance(body, Mapping):
        return None
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) > MAX_BODY_BYTES:
        return None
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def build_mail_reader(settings: Settings, redis: Redis | None = None) -> MailReader | None:
    """Build the production mailbox reader, or ``None`` when Gmail is not set up.

    ``None`` rather than an exception, because "Gmail is not configured" is the
    ordinary state of an install that has not been authorised. The runner turns
    it into a ``disabled`` source result: not attempted, nobody's fault, no
    failure counted against a board that did nothing wrong.

    Args:
        settings: Configuration.
        redis: The client whose lock serialises token refresh. ``None`` means
            refreshes are unserialised, which is correct for a one-shot CLI
            invocation and wrong for the scheduler — which always has one.

    Returns:
        A reader, or ``None``.
    """
    if not settings.gmail_configured:
        log.info(
            "gmail_reader_unavailable",
            mail_enabled=settings.mail_enabled,
            oauth_client_configured=settings.oauth_client_configured,
        )
        return None

    from scout_careers.mail.transport import GoogleGmailTransport

    try:
        transport = GoogleGmailTransport.from_settings(settings, redis=redis)
    except MailNotConfigured as exc:
        log.info("gmail_reader_unavailable", reason=exc.error_code)
        return None
    return GmailClient(transport=transport, max_messages=settings.gmail_max_messages)


async def close_mail_reader(reader: MailReader | None) -> None:
    """Release a mailbox reader, whatever kind it is.

    Duck-typed rather than declared on the protocol. ``MailReader`` is owned by
    ``sources/`` and describes the single capability the adapter needs;
    widening it with a lifecycle method the adapter never calls would make every
    test double implement one to stay structurally compatible, in exchange for
    nothing.

    Args:
        reader: The reader, or ``None``.
    """
    if reader is None:
        return
    closer = getattr(reader, "aclose", None)
    if closer is None:
        return
    await closer()


__all__ = [
    "MAX_BODY_BYTES",
    "GmailClient",
    "GmailTransport",
    "build_mail_reader",
    "build_query",
    "close_mail_reader",
    "parse_message",
]
