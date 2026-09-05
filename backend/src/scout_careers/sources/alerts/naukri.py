"""Naukri job-alert email parser.

Sender: ``info@naukri.com``.

Structure: table rows, one per role — a title anchor, then the company, an
experience range ("3-6 Yrs"), a salary cell that is usually "Not disclosed", and
a location cell that often lists several cities. The experience and salary cells
are noise for our purposes and are filtered by the shared classifier rather than
by position, so a template that reorders its columns still parses.

Naukri's alert links are frequently redirectors carrying the destination in a
``url=`` parameter; those unwrap **offline**. The rest keep the tracking URL.
``naukri.com`` is on ``NEVER_FETCH_HOSTS`` and is never requested.
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

PLATFORM = "naukri"

#: Both the direct listing shape and the click-tracker that wraps it.
_JOB_LINK_RE = re.compile(
    r"naukri\.com/(?:job-listings|jobs)[-/]|naukri\.com/trkweb|jobseeker/.*jobid",
    re.IGNORECASE,
)

#: Naukri's own job id, where the URL exposes one. Provenance only.
_JOB_ID_RE = re.compile(r"(?:job-listings-[a-z0-9-]*?|jobid[=/])([0-9]{6,})", re.IGNORECASE)

#: "3-6 Yrs" style ranges. Kept out of the identity and put in ``extra``.
_EXPERIENCE_RE = re.compile(r"\b(\d+\s*[-–]\s*\d+)\s*(?:yrs?|years?)\b", re.IGNORECASE)


def parse(message_html: str) -> list[AlertEntry]:
    """Extract every job row from one Naukri alert email.

    Args:
        message_html: The message's HTML part.

    Returns:
        One ``AlertEntry`` per row, in document order.
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

        extra: dict[str, str] = {}
        job_id = _JOB_ID_RE.search(href)
        if job_id:
            extra["naukri_job_id"] = job_id.group(1)
        experience = _experience(lines)
        if experience:
            extra["experience_hint"] = experience

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
                extra=extra,
            )
        )
    return entries


def _experience(lines: list[str]) -> str | None:
    """Return the first "N-M Yrs" range on the card, if it carries one."""
    for line in lines:
        match = _EXPERIENCE_RE.search(line)
        if match:
            return match.group(1).replace(" ", "")
    return None


__all__ = ["PLATFORM", "parse"]
