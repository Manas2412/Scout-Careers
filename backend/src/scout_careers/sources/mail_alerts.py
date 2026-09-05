"""Job-alert email adapter (SOURCE_ADAPTERS.md §7).

**Scout Careers never fetches linkedin.com, naukri.com or indeed.com.** Not
through an API, not through a browser, not through a proxy, not with a cookie,
not once. What happens instead: the operator configures job alerts in their own
account on those services and points them at a dedicated mailbox. Those services
then send alert emails, as a normal product feature, at the operator's own
request. This adapter reads *that mailbox* — the operator's own inbox, over the
Gmail API, with the operator's own OAuth grant — and parses mail already
received.

The consequences, all deliberate:

- **It is not an HTTP adapter.** It holds no ``SourceHttpClient`` and touches no
  employer host. A ``MailReader`` is injected instead, exactly the way the HTTP
  client is injected elsewhere. The protocol is declared in ``sources/base.py``
  rather than imported from ``mail/``, so the layering rule holds and this
  module imports nothing from ``mail/``.
- **Fidelity 20.** An alert carries a title, a company, a location and a link —
  not a job description. The 50-point gap to every other adapter guarantees that
  the moment the same role appears on the employer's own board, the full record
  wins and this stub is superseded (§8).
- **Every posting is marked.** ``raw["fidelity"] = "low"`` and
  ``raw["snippet_only"] = True``, so downstream stages can see what they are
  holding without inferring it from the adapter name.
- **The link is stored, never followed.** Where a tracking redirect carries its
  destination in the query string it is unwrapped offline; where it does not,
  the tracking URL is kept and ``raw`` says so. No request is issued either way,
  and ``assert_fetch_allowed`` still governs anything that ever would be.

**Identity.** There is no upstream ID, so one is synthesised deterministically
(§2.3 rule 4):

.. code-block:: text

    external_id = sha256(
        platform | normalise_company(company) | normalise_title(title) | normalise_city(location)
    ).hexdigest()[:32]

``platform`` is ``linkedin`` / ``naukri`` / ``indeed``; the other three
components are canonicalised first, so "Bangalore" and "Bengaluru, Karnataka,
India" produce one ID and "Acme Corp" and "Acme Corporation Pvt Ltd" produce
one ID. Deliberately **not** including the URL: alert links carry per-send
tracking parameters, and a URL-derived ID would make the same role look new in
every digest. Deliberately **not** including the date: the same role legitimately
appears in several alerts and must collapse to one posting. The recipe is
therefore stable across runs for the same role, which is the property
``(source_id, external_id)`` identity depends on.

**Gmail message id** is stored as a *string* in ``raw["email_message_id"]``. The
integer foreign key to ``email_message`` is Phase 5; inventing it now would
create a column-shaped value with no column behind it.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from scout_careers.common.clock import utcnow
from scout_careers.common.errors import AdapterConfigError, ScoutError
from scout_careers.common.logging import get_logger
from scout_careers.common.types import AtsType
from scout_careers.sources._shared import company_guess, config_error, elapsed_ms
from scout_careers.sources.alerts import parse_alert
from scout_careers.sources.alerts.entry import AlertEntry
from scout_careers.sources.base import (
    MailAlertConfig,
    MailMessage,
    MailReader,
    ProbeResult,
    RawPosting,
)
from scout_careers.sources.normalise import (
    infer_seniority,
    parse_location,
    parse_relative_posted_on,
)

log = get_logger(__name__)

#: How many messages the probe reads before reporting. A probe is user-facing
#: and must feel instant; it is not a fetch.
PROBE_SAMPLE_MESSAGES = 5

#: Hints finer-grained than a day. They resolve to the day the alert was sent,
#: which is the most precise honest answer an alert email supports.
_SUB_DAY_RE = re.compile(r"just (?:now|posted)|\b\d+\+?\s*(?:minute|hour)s?\s+ago\b", re.IGNORECASE)


class MailAlertAdapter:
    """Turns job-alert emails already in the operator's mailbox into leads."""

    name: ClassVar[AtsType] = AtsType.MAIL_ALERT
    config_model: ClassVar[type[BaseModel]] = MailAlertConfig
    fidelity_rank: ClassVar[int] = 20
    default_poll_interval_minutes: ClassVar[int] = 60
    requires_detail_fetch: ClassVar[bool] = False

    def __init__(
        self,
        *,
        source_id: int,
        config: BaseModel,
        mail: MailReader,
    ) -> None:
        """Construct the adapter with an injected mail reader.

        Args:
            source_id: The source being run.
            config: A ``MailAlertConfig``.
            mail: The reader ``ingest/`` binds to the operator's mailbox. Takes
                the place of the ``http`` parameter every other adapter has:
                this one issues no HTTP requests at all, so being handed an HTTP
                client would be a capability it must not hold.

        Raises:
            AdapterConfigError: When ``config`` is not a ``MailAlertConfig``.
        """
        if not isinstance(config, MailAlertConfig):
            raise AdapterConfigError("MailAlertAdapter requires a MailAlertConfig")
        self.source_id = source_id
        self.config = config
        self.mail = mail
        #: Per-run counts for ``SourceResult.skipped``. ``parse_misses`` going
        #: non-zero is the early warning that a provider changed its template;
        #: it rides in ``skipped`` because that is the only free-form count the
        #: source result carries (§10.3).
        self.skipped: dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def parse_config(cls, raw: dict[str, Any]) -> MailAlertConfig:
        """Validate ``source.config`` JSONB into a ``MailAlertConfig``.

        Args:
            raw: The stored config object.

        Returns:
            The validated config.

        Raises:
            AdapterConfigError: When the config does not validate.
        """
        try:
            return MailAlertConfig.model_validate(raw)
        except ValidationError as exc:
            raise config_error("mail_alert", exc) from exc

    async def probe(self) -> ProbeResult:
        """Read the alert label and report whether it holds parseable mail.

        Returns:
            A ``ProbeResult``. ``sample_count`` is the number of cards parsed
            out of the recent messages — zero with ``reachable=True`` means the
            mailbox works and no alerts have arrived yet, which is a different
            fault from the mailbox being unreadable.
        """
        started = time.monotonic()
        try:
            messages = await self._list_messages(self._since(None))
        except (ScoutError, TimeoutError) as exc:
            return ProbeResult(
                reachable=False,
                latency_ms=elapsed_ms(started),
                detail=f"Alert mailbox unreadable ({type(exc).__name__})",
            )

        sample = 0
        for message in list(messages)[:PROBE_SAMPLE_MESSAGES]:
            sample += len(parse_alert(message.sender, message.html_body or ""))

        return ProbeResult(
            reachable=True,
            sample_count=sample,
            latency_ms=elapsed_ms(started),
            detail=f"{len(messages)} alert message(s) in the lookback window",
            company_name_guess=company_guess(self.config.label),
        )

    async def fetch(self, *, since: datetime | None = None) -> AsyncIterator[RawPosting]:
        """Yield one lead per distinct role found in the alert mailbox.

        Args:
            since: The earliest received time to read. Defaults to
                ``lookback_hours`` before now — deliberately wider than the poll
                interval, so a missed run does not silently lose a day of
                alerts.

        Yields:
            One ``RawPosting`` per distinct role, deduplicated within the run by
            the synthetic ``external_id``: the same role legitimately appears in
            several alerts and must collapse to one posting.
        """
        messages = await self._list_messages(self._since(since))
        seen: set[str] = set()

        for message in messages:
            entries = self._parse(message)
            for entry in entries:
                if entry.external_id in seen:
                    self._count("duplicate")
                    continue
                seen.add(entry.external_id)
                yield self._to_posting(entry, message)

    async def aclose(self) -> None:
        """Nothing adapter-local to release; the mail reader is not owned here."""

    def describe(self) -> str:
        """Return the short human string used on the Companies page and digest."""
        return f"Job alert email · {self.config.label}"

    # -- internals ---------------------------------------------------------

    def _since(self, since: datetime | None) -> datetime:
        if since is not None:
            return since
        return utcnow() - timedelta(hours=self.config.lookback_hours)

    async def _list_messages(self, since: datetime) -> Sequence[MailMessage]:
        return await self.mail.list_recent_messages(
            label=self.config.label,
            senders=self.config.senders,
            since=since,
        )

    def _parse(self, message: MailMessage) -> list[AlertEntry]:
        """Parse one message, counting and logging a template change as a miss."""
        entries = parse_alert(message.sender, message.html_body or "")
        if not entries:
            self._count("parse_misses")
            log.warning(
                "alert_parse_miss",
                source_id=self.source_id,
                adapter=self.name.value,
                # The message ID only. Never the body: it is third-party mail.
                email_message_id=message.message_id,
                sender=message.sender,
            )
        return entries

    def _to_posting(self, entry: AlertEntry, message: MailMessage) -> RawPosting:
        parsed = parse_location(entry.location_raw)
        raw: dict[str, Any] = {
            # The two flags that tell every downstream stage what this is.
            "fidelity": "low",
            "snippet_only": True,
            "platform": entry.platform,
            # A string. The integer FK to email_message is Phase 5.
            "email_message_id": message.message_id,
            "email_received_at": message.received_at.isoformat(),
            "email_sender": message.sender,
            # Carried in raw because RawPosting has no company field: company
            # resolution is ingest's job, by trigram similarity against the
            # registry (§7.3).
            "company_name": entry.company_name,
            "snippet": entry.snippet,
            "posted_hint": entry.posted_hint,
            "source_url": entry.source_url,
            "canonical_url_source": (
                "tracking_url_retained" if entry.tracking_url_retained else "unwrapped_offline"
            ),
            **entry.extra,
        }

        return RawPosting(
            external_id=entry.external_id,
            url=entry.canonical_url,
            title=entry.title,
            description_html=None,
            description_text=_stub(entry, message),
            department=None,
            location_raw=entry.location_raw,
            location_city=parsed.city,
            location_country=parsed.country,
            is_remote=entry.is_remote or parsed.is_remote,
            employment_type="unknown",
            seniority_guess=infer_seniority(entry.title),
            # Resolved against the message's own received time, never now(), and
            # left None when the card carried no usable hint.
            posted_at=_posted_at(entry, message),
            raw=raw,
        )

    def _count(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _stub(entry: AlertEntry, message: MailMessage) -> str:
    """Build the structured no-description stub (§7.3).

    Deliberately recognisable: stage ⑤ skips extraction on it, because running
    the extractor over five lines would produce confident, worthless
    requirements and then score against them — worse than not scoring at all.

    Args:
        entry: The parsed card.
        message: The message it came from.

    Returns:
        The stub text. Never empty, so the DTO's ``min_length=1`` holds.
    """
    received = message.received_at.date().isoformat()
    lines = [
        f"Discovered via {entry.platform} job alert email received {received}.",
        f"Title: {entry.title}",
        f"Company: {entry.company_name}",
        f"Location: {entry.location_raw or 'not stated'}",
    ]
    if entry.snippet:
        lines.append(f"Snippet: {entry.snippet}")
    lines.append("No job description available from this source. Open the posting to read it.")
    return "\n".join(lines)


def _posted_at(entry: AlertEntry, message: MailMessage) -> datetime | None:
    """Resolve the card's relative date hint against the message's received time.

    Args:
        entry: The parsed card.
        message: The message it came from.

    Returns:
        A UTC datetime, or ``None`` when the card gave no resolvable hint. Never
        fabricated as ``now()`` — ``job_posting.first_seen_at`` already records
        when we saw it.
    """
    hint = (entry.posted_hint or "").strip().lower()
    if not hint:
        return None
    if _SUB_DAY_RE.search(hint):
        # "3 hours ago", "just now": within the day the alert was sent.
        hint = "today"
    return parse_relative_posted_on(f"Posted {hint}", message.received_at)


__all__ = ["PROBE_SAMPLE_MESSAGES", "MailAlertAdapter"]
