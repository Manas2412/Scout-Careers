"""Indeed job-alert email parser.

Senders: ``alert@indeed.com``, ``noreply@indeed.com``.

Structure: one block per job — a title anchor, then a company/location line and
usually a one-sentence snippet. Indeed routes almost every link through
``cts.indeed.com``, whose destination is percent-encoded in a ``url=``
parameter; that unwraps **offline** and often lands on the employer's own ATS,
which is the best outcome this adapter can produce. Where it does not, the
``rc/clk?jk=`` link is retained as-is.

``indeed.com`` is on ``NEVER_FETCH_HOSTS``. No link here is ever followed to
resolve a hop — the unwrap is pure string work on the query string.
"""

from __future__ import annotations

import re

from scout_careers.sources.alerts.entry import (
    AlertEntry,
    anchors_matching,
    card_lines,
    clean_text,
    is_remote_location,
    parse_tree,
    posted_hint,
    split_card,
    synthesise_external_id,
    unwrap_tracking_url,
)

PLATFORM = "indeed"

#: The click-through shapes: the tracker, the classic redirect and the viewjob
#: permalink.
_JOB_LINK_RE = re.compile(
    r"(?:cts\.)?indeed\.com/(?:v\d+/[^\s\"']*|rc/clk|pagead/clk|viewjob)",
    re.IGNORECASE,
)

#: Indeed's job key. Provenance only; identity is synthesised.
_JOB_KEY_RE = re.compile(r"[?&](?:jk|vjk)=([0-9a-f]{8,})", re.IGNORECASE)


def parse(message_html: str) -> list[AlertEntry]:
    """Extract every job block from one Indeed alert email.

    Args:
        message_html: The message's HTML part.

    Returns:
        One ``AlertEntry`` per block, in document order.
    """
    tree = parse_tree(message_html)
    entries: list[AlertEntry] = []
    seen: set[str] = set()

    for anchor in anchors_matching(tree, _JOB_LINK_RE):
        title = clean_text(anchor.text(separator=" "))
        if not title:
            continue
        href = anchor.attributes.get("href") or ""
        lines = card_lines(anchor)
        company, location, snippet = split_card(lines)
        if not company:
            continue

        canonical_url, unwrapped = unwrap_tracking_url(href)
        external_id = synthesise_external_id(
            platform=PLATFORM,
            company_name=company,
            title=title,
            location=location or "",
        )
        if external_id in seen:
            continue
        seen.add(external_id)

        job_key = _JOB_KEY_RE.search(href)
        entries.append(
            AlertEntry(
                title=title,
                company_name=company,
                location_raw=location,
                is_remote=is_remote_location(location),
                canonical_url=canonical_url,
                external_id=external_id,
                snippet=snippet,
                posted_hint=posted_hint(lines),
                platform=PLATFORM,
                tracking_url_retained=not unwrapped,
                source_url=href,
                extra={"indeed_job_key": job_key.group(1)} if job_key else {},
            )
        )
    return entries


__all__ = ["PLATFORM", "parse"]
