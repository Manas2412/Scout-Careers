"""Per-provider job-alert email parsers, and the dispatcher over them.

One parser per sender family, each a pure function from message HTML to a list
of :class:`~scout_careers.sources.alerts.entry.AlertEntry`. Parsers work on the
**HTML part** via ``selectolax`` using structural selectors only. They never
execute anything, never load a remote image — which would ping the sender's
tracking pixel — and treat every extracted string as untrusted text
(SOURCE_ADAPTERS.md §7.3).

None of them fetch anything. ``linkedin.com``, ``naukri.com`` and ``indeed.com``
are on ``NEVER_FETCH_HOSTS`` and this package holds no HTTP client at all.
"""

from __future__ import annotations

from collections.abc import Callable

from scout_careers.sources.alerts import indeed, linkedin, naukri
from scout_careers.sources.alerts.entry import (
    AlertEntry,
    normalise_city,
    normalise_company,
    strip_tracking,
    synthesise_external_id,
    unwrap_tracking_url,
)

#: Sender-domain fragment → parser. Matched as a substring of the lower-cased
#: sender, so both ``jobalerts-noreply@linkedin.com`` and
#: ``jobs-listings@linkedin.com`` land on the same parser without enumerating
#: every mailbox LinkedIn has ever sent from.
PARSERS: dict[str, Callable[[str], list[AlertEntry]]] = {
    "linkedin.com": linkedin.parse,
    "naukri.com": naukri.parse,
    "indeed.com": indeed.parse,
}


def parse_alert(sender: str, html: str) -> list[AlertEntry]:
    """Parse one alert message with the parser for its sender.

    Args:
        sender: The message's ``From`` address.
        html: The message's HTML part.

    Returns:
        The cards found, or an empty list when the sender matches no parser or
        the template changed. An empty list is never an error here: the adapter
        counts it as a parse miss and logs the message ID, which is the early
        warning that a provider changed its template.
    """
    parser = parser_for(sender)
    if parser is None or not html:
        return []
    return parser(html)


def parser_for(sender: str) -> Callable[[str], list[AlertEntry]] | None:
    """Return the parser for a sender address, or ``None``.

    Args:
        sender: The message's ``From`` address, in any case.

    Returns:
        The matching parser, or ``None`` when no provider is recognised.
    """
    lowered = sender.strip().lower()
    for domain, parser in PARSERS.items():
        if domain in lowered:
            return parser
    return None


__all__ = [
    "PARSERS",
    "AlertEntry",
    "normalise_city",
    "normalise_company",
    "parse_alert",
    "parser_for",
    "strip_tracking",
    "synthesise_external_id",
    "unwrap_tracking_url",
]
