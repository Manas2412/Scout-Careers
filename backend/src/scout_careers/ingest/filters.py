"""Stage ④: the deterministic filter. Pure boolean, zero model calls.

The stage the cost model rests on. Extraction costs one model call per posting;
this decides how many postings there are. Gate 2.6 wants ≥ 70% removed here, and
the arithmetic is unforgiving — the difference between a 60% and an 80% kill
rate on 7,000 postings is roughly a factor of two on the whole backfill bill.

**First rejection wins.** ``filter_reason`` names the *first* disqualifying
reason, not an arbitrary one, so "why was this dropped" has one answer and the
cheapest predicates run first. Ordering is therefore semantic, not just an
optimisation.

**Filtering never deletes.** It writes ``filtered_out = true`` and a reason, so
a filter mistake is discoverable — ``GET /postings?status=filtered`` shows what
was dropped and why. A silently discarded role is a role the operator cannot
learn they are missing.

Two places where the docs disagree, resolved here and worth knowing about:

1. **Seniority is a deny-list, not an allow-list.** SDD.md §3.2 shows
   ``filter_seniority_allowed``; CONFIGURATION.md §8 documents
   ``FILTER_SENIORITY_DENY`` with default ``intern,director,executive``.
   CONFIGURATION.md declares itself canonical for "every environment variable,
   its type, default and requirement", so the deny-list wins. The two agree on
   behaviour anyway: SDD requires ``unknown`` to pass, and ``unknown`` is not in
   a deny-list.

2. **The keyword deny-list matches titles only.** SDD.md §3.2 has two
   predicates — one on the title, one whole-word over ``description_text`` —
   but CONFIGURATION.md defines a single ``FILTER_KEYWORD_DENY`` and describes
   it as "Title keywords killed at stage ④". Title-only is both what the
   canonical config document says and the safer reading: the default list
   contains ``firmware``, and a backend role whose description happens to
   mention firmware once is not a firmware job. Applying it to descriptions
   would have silently dropped the two Seagate roles this project started from.

SDD.md's ``employment_type`` predicate has no key in CONFIGURATION.md §8 and is
therefore not implemented; adding it means adding its configuration first.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from scout_careers.common.config import Settings
from scout_careers.common.types import CompanyStatus

#: ``ingest/dedup.py``'s marker. A superseded posting is already represented by
#: a better record of the same role, so extracting it would buy a second copy of
#: an answer we hold.
SUPERSEDED_PREFIX: Final = "superseded_by:"

#: Below this, ``description_text`` is a stub rather than a job description.
#: A mail-alert stub is ~200 characters of "Discovered via linkedin job alert";
#: the shortest genuine JD in the live corpus runs to several hundred. Set where
#: it separates those two populations rather than at a round number.
MIN_DESCRIPTION_CHARS: Final = 400


@dataclass(frozen=True, slots=True)
class FilterVerdict:
    """The outcome of one predicate, or of the whole chain.

    Attributes:
        passed: Whether the posting survives.
        reason: Written to ``job_posting.filter_reason``. ``None`` on a pass.
    """

    passed: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PostingView:
    """The posting fields stage ④ reads, and nothing else.

    A view rather than the ORM row so the predicates are pure functions over
    plain data: they are tested without a database, and a change to
    ``models.py`` cannot quietly change what the filter looks at.
    """

    id: str
    title: str
    description_text: str
    location_city: str | None = None
    location_country: str | None = None
    is_remote: bool = False
    seniority_guess: str | None = None
    closed_at: datetime | None = None
    filtered_out: bool = False
    filter_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompanyView:
    """The company fields stage ④ reads."""

    id: int
    slug: str
    status: CompanyStatus = CompanyStatus.TRACKING
    location_filter: Sequence[str] = ()


Predicate = Callable[[PostingView, CompanyView, Settings], FilterVerdict]

PASS = FilterVerdict(True)


def _fail(reason: str) -> FilterVerdict:
    return FilterVerdict(False, reason)


# ---------------------------------------------------------------------------
# Years of experience, read from the description
# ---------------------------------------------------------------------------

#: One or two digits. Nothing sensible says "100 years of experience", and the
#: bound keeps a stray salary or a phone number out of the match.
_NUM = r"(\d{1,2})"

#: "3-5", "3 to 5", "3+ to 5". Captured so the low end can be preferred.
_RANGE = rf"{_NUM}\s*(?:\+|plus)?\s*(?:[-–—]|to)\s*{_NUM}"

_YEARS = r"(?:years?|yrs?)"

#: A bare "5 years" is not a requirement — "we have grown for 5 years" and
#: "15 years of company history" both match it. One of these words has to sit
#: near the number for it to count as an ask.
_CONTEXT = (
    r"experience|exp\b|background|working|industry|professional|relevant|"
    r"hands.on|building|developing|engineering|in\s+a\s+similar"
)

_YEARS_RE = re.compile(rf"(?:{_RANGE}|{_NUM}\s*\+?)\s*{_YEARS}", re.IGNORECASE)
_CONTEXT_RE = re.compile(_CONTEXT, re.IGNORECASE)

#: How far either side of the number to look for one of those words.
_CONTEXT_AFTER: Final = 80
_CONTEXT_BEFORE: Final = 40

#: Above this, the figure is describing the company rather than the candidate.
_MAX_PLAUSIBLE_YEARS: Final = 30


def parse_experience_years(description: str) -> int | None:
    """Return the least demanding stated experience requirement, in years.

    Args:
        description: ``job_posting.description_text``, untrusted employer prose.

    Returns:
        The minimum number of years any stated requirement asks for, or ``None``
        when the description states none. ``0`` is a real answer and is distinct
        from ``None`` — "0-2 years of experience" is a new-graduate role, which
        is the opposite of an unstated one.

    Deliberately literal. It reads digits near a word like "experience" and does
    no inference: "Five years" returns ``None`` rather than five, because a
    parser that starts interpreting is one that starts being wrong quietly, and
    an unstated requirement is kept rather than dropped.
    """
    found: list[int] = []
    for match in _YEARS_RE.finditer(description):
        after = description[match.end() : match.end() + _CONTEXT_AFTER]
        before = description[max(0, match.start() - _CONTEXT_BEFORE) : match.start()]
        if not (_CONTEXT_RE.search(after) or _CONTEXT_RE.search(before)):
            continue
        # Group 1 is a range's low end; group 3 is the standalone form.
        low = match.group(1) or match.group(3)
        if low is None:
            continue
        years = int(low)
        if 0 <= years <= _MAX_PLAUSIBLE_YEARS:
            found.append(years)
    return min(found) if found else None


# ---------------------------------------------------------------------------
# Predicates, cheapest first
# ---------------------------------------------------------------------------


def _company_not_blacklisted(
    posting: PostingView, company: CompanyView, settings: Settings
) -> FilterVerdict:
    """A decision about an employer outranks everything about the role."""
    del posting, settings
    if company.status is CompanyStatus.BLACKLISTED:
        return _fail("company_blacklisted")
    return PASS


def _not_superseded(
    posting: PostingView, company: CompanyView, settings: Settings
) -> FilterVerdict:
    """A duplicate of a record we already hold at higher fidelity."""
    del company, settings
    if posting.filtered_out and (posting.filter_reason or "").startswith(SUPERSEDED_PREFIX):
        return _fail("superseded")
    return PASS


def _not_closed(posting: PostingView, company: CompanyView, settings: Settings) -> FilterVerdict:
    """A role that is no longer listed cannot be applied to."""
    del company, settings
    if posting.closed_at is not None:
        return _fail("closed")
    return PASS


def _has_usable_description(
    posting: PostingView, company: CompanyView, settings: Settings
) -> FilterVerdict:
    """Refuse to extract requirements from something that has none.

    Two cases. ``needs_description`` is set by ``ingest/resolve.py`` on every
    mail-alert lead — a title, a company and a link, no body. A short body is
    the same situation arrived at differently.

    This is the predicate that protects the extractor from itself: run over a
    stub it does not fail, it invents. Confident, wrong requirements then
    produce a confident, wrong coverage score, which is worse than no score,
    because the operator cannot tell the difference by looking.
    """
    del company
    if posting.raw.get("needs_description") and not settings.alert_fidelity_extract:
        return _fail("no_description")
    if len(posting.description_text.strip()) < MIN_DESCRIPTION_CHARS:
        return _fail("description_too_short")
    return PASS


def _seniority_allowed(
    posting: PostingView, company: CompanyView, settings: Settings
) -> FilterVerdict:
    """Drop bands the operator is not applying into.

    ``unknown`` passes. The seniority inference already resolves ambiguity to
    ``mid``, so a residual ``unknown`` means the source stated nothing — which
    is not grounds for dropping a role.
    """
    del company
    guess = (posting.seniority_guess or "unknown").strip().lower()
    if guess in {entry.lower() for entry in settings.filter_seniority_deny}:
        return _fail(f"seniority:{guess}")
    return PASS


def _location_matches(
    posting: PostingView, company: CompanyView, settings: Settings
) -> FilterVerdict:
    """COMPANY_REGISTRY.md §5.2.

    Positive tokens OR together; a negative token (``!Chennai``) vetoes outright
    regardless of any positive match. A company's own ``location_filter``
    **overrides** the global default entirely rather than intersecting with it —
    "for this employer, only these places" is the thing an operator means.

    An unresolvable location passes a non-empty filter. Deliberate and
    asymmetric: a false positive costs two seconds of attention, a false
    negative is a role that is never seen and never known to have been missed.
    """
    tokens = list(company.location_filter) or list(settings.default_location_filter)
    if not tokens:
        return PASS

    positives = [t for t in tokens if not t.startswith("!")]
    negatives = [t[1:] for t in tokens if t.startswith("!")]

    for token in negatives:
        if _location_token_matches(token, posting):
            return _fail(f"location_excluded:{token.lower()}")

    if not positives:
        return PASS
    if posting.location_city is None and posting.location_country is None and not posting.is_remote:
        return PASS
    if any(_location_token_matches(token, posting) for token in positives):
        return PASS
    return _fail("location")


def _location_token_matches(token: str, posting: PostingView) -> bool:
    """Match one filter token against a posting's place.

    ``remote`` is a token about arrangement rather than geography and is checked
    against the flag. Everything else is compared to the country code and the
    city, case-insensitively.
    """
    needle = token.strip().lower()
    if not needle:
        return False
    if needle == "remote":
        return posting.is_remote
    if (posting.location_country or "").strip().lower() == needle:
        return True
    return (posting.location_city or "").strip().lower() == needle


def _title_not_denied(
    posting: PostingView, company: CompanyView, settings: Settings
) -> FilterVerdict:
    """Whole-word, case-insensitive deny-list over the title.

    Whole-word matters: ``sales`` must not kill "Sales**force** Engineer", and
    a multi-word entry like ``device driver`` has to match as a phrase. Both are
    handled by bounding the escaped entry with ``\\b``.

    **Longest entry first**, which is not cosmetic. The deny-list contains both
    ``driver`` and ``device driver``, and in file order the shorter one matches
    "Device Driver Engineer" first — dropping the role correctly but recording
    ``title_keyword:driver``, which reads as a delivery job. The reason column
    is the only explanation the operator ever gets for a posting they did not
    see, so it has to name the rule that actually applies.
    """
    del company
    title = posting.title.lower()
    for needle in _deny_entries_by_specificity(settings.filter_keyword_deny):
        if re.search(rf"\b{re.escape(needle)}\b", title):
            return _fail(f"title_keyword:{needle}")
    return PASS


def _deny_entries_by_specificity(entries: Sequence[str]) -> list[str]:
    """Normalise deny-list entries and order them most-specific first.

    Args:
        entries: The configured list, in whatever order it was written.

    Returns:
        Trimmed, lower-cased, non-empty entries sorted by descending length
        then alphabetically, so the ordering is total and the reason a posting
        is dropped does not depend on how the ``.env`` line was typed.
    """
    cleaned = {entry.strip().lower() for entry in entries if entry.strip()}
    return sorted(cleaned, key=lambda entry: (-len(entry), entry))


def _experience_within_reach(
    posting: PostingView, company: CompanyView, settings: Settings
) -> FilterVerdict:
    """Drop roles whose *least* demanding stated requirement is out of reach.

    The predicate that reads what a role actually asks for instead of guessing
    from its name. A title deny-list gets "Senior Software Engineer" wrong in
    both directions — some want two years, some want ten — and it cannot tell a
    Solutions Architect role open to a second-year engineer from one wanting a
    decade. The job description usually just says.

    **The minimum wins, not the maximum.** A description saying "8+ years of
    engineering experience" and "3+ years with Go" is kept, because 3 is within
    reach and the operator can judge the rest. Only when *every* stated figure
    exceeds the ceiling is the role beyond them, and that is a much safer claim
    than picking whichever number looks like the headline.

    **Unstated passes**, and is not silently forgotten: the years figure is
    returned by :func:`parse_experience_years` so a caller can record it, and a
    posting with no stated requirement is one the operator reads for themselves.
    Roughly the same asymmetry as the location rule — a false positive costs a
    few seconds, a false negative is invisible.

    Set ``FILTER_MAX_YEARS_EXPERIENCE=0`` to switch the predicate off.
    """
    del company
    ceiling = settings.filter_max_years_experience
    if ceiling <= 0:
        return PASS
    required = parse_experience_years(posting.description_text)
    if required is not None and required > ceiling:
        return _fail(f"experience:{required}y")
    return PASS


#: Ordered cheapest-first. The order is part of the contract: it decides which
#: reason a posting is recorded with when several apply.
CHAIN: Final[tuple[tuple[str, Predicate], ...]] = (
    ("company_status", _company_not_blacklisted),
    ("superseded", _not_superseded),
    ("closed", _not_closed),
    ("no_description", _has_usable_description),
    ("seniority", _seniority_allowed),
    ("location", _location_matches),
    ("title_denylist", _title_not_denied),
    # Last: the only predicate that scans the whole description.
    ("experience", _experience_within_reach),
)


def evaluate(posting: PostingView, company: CompanyView, settings: Settings) -> FilterVerdict:
    """Run the chain, stopping at the first rejection.

    Args:
        posting: The posting under test.
        company: Its employer.
        settings: Configuration supplying the three lists.

    Returns:
        A passing verdict, or the first failure with its reason.
    """
    for name, predicate in CHAIN:
        verdict = predicate(posting, company, settings)
        if not verdict.passed:
            return FilterVerdict(False, verdict.reason or name)
    return PASS


__all__ = [
    "CHAIN",
    "MIN_DESCRIPTION_CHARS",
    "SUPERSEDED_PREFIX",
    "CompanyView",
    "FilterVerdict",
    "PostingView",
    "Predicate",
    "evaluate",
    "parse_experience_years",
]
