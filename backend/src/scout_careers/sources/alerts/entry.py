"""The alert DTO, the synthetic identity, and the shared parsing primitives.

Every function here is pure: HTML in, values out. Nothing fetches, nothing
resolves a redirect over the wire, and nothing executes anything the message
contains. Alert HTML is mail from a third party and is treated as untrusted text
throughout (``ARCHITECTURE.md`` §2).

This module exists separately from ``alerts/__init__.py`` so the per-provider
parsers can import the DTO without importing the dispatcher that imports them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from selectolax.parser import HTMLParser, Node

from scout_careers.sources.normalise import normalise_title, parse_location

#: Query parameters that carry the real destination on a tracking redirect. All
#: of these are resolvable **offline**, from the query string alone — which is
#: the only way this system ever unwraps a link. A redirect that can only be
#: resolved by issuing a request is left alone: the hosts involved are on
#: ``NEVER_FETCH_HOSTS`` and there is no exception to that.
TARGET_PARAMS: Final[tuple[str, ...]] = ("url", "u", "target", "destination", "redirect", "r")

#: Stripped from a retained URL. Per-send tracking noise, never part of identity.
#: Compared case-insensitively — providers mix ``trackingId`` and ``trackingid``
#: in the same message.
TRACKING_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "trk",
        "trkemail",
        "trackingid",
        "refid",
        "midtoken",
        "midsig",
        "eid",
        "otptoken",
        "lipi",
        "licu",
        "lici",
        "from",
        "srcid",
        "src",
        "tk",
        "atk",
    }
)

_UTM_RE = re.compile(r"^utm_", re.IGNORECASE)

#: Legal-form suffixes dropped before a company name enters the synthetic ID, so
#: "Acme Corp" and "Acme Corporation Pvt Ltd" collapse to one posting.
_COMPANY_SUFFIXES: Final[tuple[str, ...]] = (
    "private limited",
    "pvt ltd",
    "pvt. ltd.",
    "pvt",
    "limited",
    "ltd",
    "llc",
    "llp",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "company",
    "co",
    "gmbh",
    "bv",
    "nv",
    "plc",
    "sa",
    "ag",
    "technologies",
    "technology",
    "labs",
    "india",
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_WS_RE = re.compile(r"\s+")

#: Lines an alert card carries that are neither the company nor the location:
#: engagement badges, salary bands, experience ranges and relative dates. Dates
#: are still read out of the *unfiltered* lines by :func:`posted_hint`, so
#: classifying them as noise here loses nothing.
_NOISE_RE = re.compile(
    r"^(?:"
    r"easy apply|actively hiring|be an early applicant|promoted|viewed|new"
    r"|apply now|view job|see job|saved? job|be one of the first|actively recruiting"
    r"|\d[\d,]*\s+(?:applicants?|connections?|alumni)\b.*"
    r"|\d+\s*[-\u2013]\s*\d+\s*(?:yrs?|years?)\b.*"
    r"|not disclosed.*|\u20b9.*|rs\.?\s*\d.*|\$\s*[\d,]+.*"
    r"|posted\b.*"
    r"|\d+\+?\s*(?:minute|hour|day|week|month)s?\s+ago\b.*"
    r"|just (?:now|posted)|today|yesterday"
    r")\s*$",
    re.IGNORECASE,
)

#: A card line only counts as a snippet once it is sentence-length; shorter
#: lines are badges, seniority chips and "3 school alumni work here".
SNIPPET_MIN_CHARS: Final[int] = 40

#: A card that has yielded this many lines has yielded its company and location;
#: climbing further would swallow the neighbouring card.
CARD_MIN_LINES: Final[int] = 2

_POSTED_HINT_RE = re.compile(
    r"\b(today|yesterday|just (?:now|posted)|\d+\+?\s*(?:minute|hour|day|week|month)s?\s+ago)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AlertEntry:
    """One job card lifted out of one alert email.

    A lead, not a posting: there is a title, a company, a location and a link,
    and no description. That asymmetry is why ``mail_alert`` sits at fidelity 20
    (SOURCE_ADAPTERS.md §8).

    Attributes:
        title: The role title, verbatim from the card.
        company_name: The employer name as the alert spelled it. Resolved
            against the registry by trigram similarity in ``ingest/``.
        location_raw: The location line, verbatim.
        is_remote: Whether the card carried a remote marker.
        canonical_url: The employer-facing URL where one could be unwrapped
            offline, otherwise the tracking URL, retained verbatim.
        external_id: The deterministic synthetic ID (see
            :func:`synthesise_external_id`).
        snippet: A short excerpt if the card had one. Never a description.
        posted_hint: The card's relative date phrase, e.g. ``"2 days ago"``.
        platform: ``linkedin`` | ``naukri`` | ``indeed``.
    """

    title: str
    company_name: str
    location_raw: str | None
    is_remote: bool
    canonical_url: str
    external_id: str
    snippet: str | None = None
    posted_hint: str | None = None
    platform: str = "unknown"
    #: True when ``canonical_url`` is still the sender's tracking redirect.
    tracking_url_retained: bool = False
    #: The original link as it appeared in the message, kept for provenance.
    source_url: str = ""
    #: Populated by parsers that want extra provenance in ``raw``.
    extra: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Identity (SOURCE_ADAPTERS.md §2.3 rule 4, §7.3)
# ---------------------------------------------------------------------------

EXTERNAL_ID_LENGTH: Final[int] = 32


def normalise_company(name: str) -> str:
    """Canonicalise a company name for the synthetic ID.

    Args:
        name: The company name as the alert spelled it.

    Returns:
        A lower-case, punctuation-free key with legal-form suffixes removed.
    """
    text = _WS_RE.sub(" ", name.strip().lower())
    text = _NON_ALNUM_RE.sub(" ", text).strip()
    changed = True
    while changed and text:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -(len(suffix) + 1)].strip()
                changed = True
    return _WS_RE.sub(" ", text).strip()


def normalise_city(location: str | None) -> str:
    """Canonicalise a location for the synthetic ID.

    Resolves through the curated city table, so "Bangalore" and "Bengaluru,
    Karnataka, India" produce the same key and one role does not become two
    postings across two alerts.

    Args:
        location: The location line, or ``None``.

    Returns:
        A lower-case city key, ``"remote"`` when only a remote marker resolved,
        or ``""`` when nothing did.
    """
    parsed = parse_location(location)
    if parsed.city:
        return parsed.city.lower()
    if parsed.is_remote:
        return "remote"
    if location:
        return _NON_ALNUM_RE.sub(" ", location.strip().lower()).strip()
    return ""


def synthesise_external_id(*, platform: str, company_name: str, title: str, location: str) -> str:
    """Derive the deterministic ``external_id`` for a mail-sourced posting.

    The recipe, and the two deliberate omissions:

    ``sha256(platform | company | title | city)[:32]``

    - **The URL is not in it.** Alert links carry per-send tracking parameters,
      so a URL-derived ID would make the same role look new in every digest.
    - **The date is not in it.** The same role legitimately appears in several
      alerts on several days and must collapse to one posting.

    Every component is canonicalised first, so the same role spelled slightly
    differently across two providers' templates still lands on one ID.

    Args:
        platform: ``linkedin`` | ``naukri`` | ``indeed``.
        company_name: The employer name from the card.
        title: The role title from the card.
        location: The location line from the card.

    Returns:
        A 32-character lower-case hex string, stable across runs.
    """
    material = "|".join(
        [
            platform.strip().lower(),
            normalise_company(company_name),
            normalise_title(title),
            normalise_city(location),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:EXTERNAL_ID_LENGTH]


# ---------------------------------------------------------------------------
# URL handling — offline only
# ---------------------------------------------------------------------------


def strip_tracking(url: str) -> str:
    """Remove per-send tracking parameters from a URL, leaving it addressable.

    Args:
        url: An absolute URL.

    Returns:
        The same URL without ``utm_*`` and the known tracking keys.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not _UTM_RE.match(key)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def unwrap_tracking_url(url: str) -> tuple[str, bool]:
    """Resolve a tracking redirect to the employer URL, **without fetching**.

    Only the offline case is handled: a redirector that carries its destination
    in the query string. Anything else keeps the tracking URL — the alternative
    would be issuing a request to resolve the hop, and every one of these hosts
    is on ``NEVER_FETCH_HOSTS``. ``assert_fetch_allowed`` would refuse it
    anyway; not attempting it is the honest version of the same rule.

    Args:
        url: The link as it appeared in the message.

    Returns:
        ``(resolved_url, unwrapped)``. ``unwrapped`` is False when the tracking
        URL was retained, which the adapter records in ``raw``.
    """
    seen: set[str] = set()
    current = url
    unwrapped = False
    # Bounded: a redirector whose target is itself a redirector is unwrapped at
    # most a few times, and a cycle cannot spin.
    for _hop in range(4):
        if current in seen:
            break
        seen.add(current)
        target = _query_target(current)
        if target is None:
            break
        current = target
        unwrapped = True
    return strip_tracking(current), unwrapped


def _query_target(url: str) -> str | None:
    """Return the absolute http(s) destination encoded in a URL's query, if any."""
    parts = urlsplit(url)
    if not parts.query:
        return None
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key in TARGET_PARAMS:
        candidate = params.get(key)
        if not candidate:
            continue
        decoded = unquote(candidate).strip()
        decoded_parts = urlsplit(decoded)
        if decoded_parts.scheme in {"http", "https"} and decoded_parts.netloc:
            return decoded
    return None


# ---------------------------------------------------------------------------
# Shared card-reading primitives
# ---------------------------------------------------------------------------


def parse_tree(message_html: str) -> HTMLParser:
    """Parse message HTML, dropping everything that is not text structure.

    ``script``/``style``/``img`` are removed: the first two are never executed
    but must not reach a prompt either, and an ``img`` in an alert email is the
    sender's tracking pixel, which is never loaded and never looked at.

    Args:
        message_html: The message's HTML part.

    Returns:
        A parsed tree.
    """
    tree = HTMLParser(message_html)
    for node in tree.css("script,style,noscript,iframe,img,template"):
        node.decompose()
    return tree


def anchors_matching(tree: HTMLParser, pattern: re.Pattern[str]) -> list[Node]:
    """Return the anchors whose ``href`` matches ``pattern``, in document order.

    Args:
        tree: The parsed message.
        pattern: A compiled pattern applied to the raw ``href``.

    Returns:
        The matching ``<a>`` nodes.
    """
    matched: list[Node] = []
    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        if pattern.search(href):
            matched.append(node)
    return matched


def card_lines(anchor: Node, *, max_levels: int = 6) -> list[str]:
    """Return the text lines of the card containing ``anchor``, title excluded.

    Walks up from the anchor to the nearest ancestor that reads like a card —
    a table row, a table or a list item — and flattens it. Structural only: no
    class names, no ids, nothing a provider changes every quarter.

    Args:
        anchor: The title anchor.
        max_levels: How far up to look before giving up.

    Returns:
        The card's non-empty lines with the title line removed.
    """
    title = clean_text(anchor.text(separator=" "))
    node: Node | None = anchor.parent
    best: list[str] = []
    for _level in range(max_levels):
        if node is None:
            break
        lines = [clean_text(line) for line in node.text(separator="\n").split("\n")]
        lines = [line for line in lines if line and line != title]
        if len(lines) > len(best):
            best = lines
        if node.tag in {"tr", "table", "li"} and len(best) >= CARD_MIN_LINES:
            break
        node = node.parent
    return best


def clean_text(value: str) -> str:
    """Collapse whitespace, including the non-breaking spaces mail is full of."""
    return _WS_RE.sub(" ", value.replace("\u00a0", " ")).strip()


def is_noise(line: str) -> bool:
    """Report whether a card line is a badge, a salary band or an experience range."""
    return bool(_NOISE_RE.match(line))


def looks_like_location(line: str) -> bool:
    """Report whether a line resolves to a city, a country or a remote marker."""
    parsed = parse_location(line)
    return bool(parsed.city or parsed.country or parsed.is_remote)


def is_remote_location(line: str | None) -> bool:
    """Report whether a location line carries a remote marker."""
    return parse_location(line).is_remote


def posted_hint(lines: list[str]) -> str | None:
    """Return the first relative-date phrase among ``lines``, if any."""
    for line in lines:
        match = _POSTED_HINT_RE.search(line)
        if match:
            return match.group(0)
    return None


def split_card(lines: list[str]) -> tuple[str | None, str | None, str | None]:
    """Classify a card's lines into company, location and snippet.

    All three providers order a card the same way — title, company, location,
    then anything else — so the company is the first line that is not a badge,
    a salary band, an experience range or a date. The location is not taken
    positionally: it is the first line *after* the company that actually
    resolves to a city, a country or a remote marker, so a template that slips
    an extra row in still parses.

    Taking the company positionally and the location by resolution is the way
    round that survives real data: an employer legitimately called "Zeta India"
    resolves as a location, and treating the first resolving line as the
    location would file the company name as the city.

    Args:
        lines: The card's lines, title excluded.

    Returns:
        ``(company, location, snippet)``, each possibly ``None``.
    """
    company: str | None = None
    location: str | None = None
    snippet: str | None = None
    leftovers: list[str] = []
    for line in lines:
        if is_noise(line):
            continue
        if company is None:
            company = line
            continue
        if location is None and looks_like_location(line):
            location = line
            continue
        if snippet is None and len(line) >= SNIPPET_MIN_CHARS:
            snippet = line
            continue
        leftovers.append(line)
    if location is None and leftovers:
        # Nothing resolved, so the line is kept verbatim and the city stays
        # null. "Mumbai (All Areas)" is exactly this case, and §9.2 preserves
        # the raw string rather than guessing.
        location = leftovers[0]
    return company, location, snippet


__all__ = [
    "EXTERNAL_ID_LENGTH",
    "TARGET_PARAMS",
    "TRACKING_PARAMS",
    "AlertEntry",
    "anchors_matching",
    "card_lines",
    "clean_text",
    "is_noise",
    "is_remote_location",
    "looks_like_location",
    "normalise_city",
    "normalise_company",
    "parse_tree",
    "posted_hint",
    "split_card",
    "strip_tracking",
    "synthesise_external_id",
    "unwrap_tracking_url",
]
