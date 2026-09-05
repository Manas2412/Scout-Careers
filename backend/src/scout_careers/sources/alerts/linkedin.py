"""LinkedIn job-alert email parser.

Senders: ``jobalerts-noreply@linkedin.com``, ``jobs-listings@linkedin.com``.

Structure: repeated job-card table blocks. The title is the anchor text on a
link into ``/comm/jobs/view/{id}``; the company and location follow as text
nodes inside the same card. Selection is structural — the anchor's href shape
and the enclosing table — never a class name or an inline style, both of which
LinkedIn regenerates constantly.

**This parser reads an email the operator already received. It never fetches
linkedin.com** (SOURCE_ADAPTERS.md §7.1). The alert link is unwrapped offline
where the destination is in the query string, stored either way, and never
followed.
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

PLATFORM = "linkedin"

#: The job-view link shape. ``/comm/`` is the email variant of ``/jobs/view/``.
_JOB_LINK_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/", re.IGNORECASE)

#: The numeric job ID inside the path. Recorded in ``extra`` for the operator,
#: never used as identity — §7.3 synthesises identity so that the same role seen
#: through two providers collapses to one posting.
_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)")


def parse(message_html: str) -> list[AlertEntry]:
    """Extract every job card from one LinkedIn alert email.

    Args:
        message_html: The message's HTML part.

    Returns:
        One ``AlertEntry`` per card, in document order. Empty when the template
        changed — the caller counts that as a parse miss rather than reading it
        as "this alert had no jobs".
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
            # LinkedIn repeats a role between the "top pick" block and the list.
            continue
        seen.add(external_id)

        job_id = _JOB_ID_RE.search(href)
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
                extra={"linkedin_job_id": job_id.group(1)} if job_id else {},
            )
        )
    return entries


__all__ = ["PLATFORM", "parse"]
