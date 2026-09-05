"""Normalisation. Adapters own it, so ``ingest/`` receives canonical DTOs.

Nothing here calls a model and nothing here calls the network. A city name is
not worth a network dependency, and a wrong guess about a location silently
removes a posting from the operator's filter — a false negative that is
invisible (SOURCE_ADAPTERS.md §9.2).
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from selectolax.parser import HTMLParser

from scout_careers.common.types import EmploymentType, Seniority

BLOCK = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
DROP = {"script", "style", "noscript", "iframe", "svg", "form", "template"}

_NBSP = "\u00a0"
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff]")


def html_to_text(raw: str | None) -> str:
    """Flatten job-description HTML to the text the extractor reads.

    ``html.unescape`` runs before parsing, always: Greenhouse serves
    entity-escaped HTML, and applying it universally costs nothing on
    already-clean input. List structure is preserved as ``"• "`` because
    requirements are almost always ``<li>`` elements and flattening them into a
    paragraph measurably degrades extraction. Nothing else is preserved: the
    extractor reads prose, not Markdown.

    ``script``/``style``/``iframe``/``form`` are removed before text extraction
    — partly for cleanliness, mostly because their content is
    attacker-controllable and must never reach a prompt.

    Args:
        raw: HTML, or ``None``.

    Returns:
        Plain text, NFKC-normalised, zero-width characters stripped, blank runs
        collapsed.
    """
    if not raw:
        return ""
    tree = HTMLParser(html.unescape(raw))  # unescape FIRST
    for node in tree.css(",".join(DROP)):
        node.decompose()
    for node in tree.css("li"):
        node.insert_before("\n• ")
    for node in tree.css(",".join(BLOCK - {"li"})):
        node.insert_before("\n")
    text = tree.text(separator="")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(_NBSP, " ")
    text = _ZERO_WIDTH_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# §9.2 Location
# ---------------------------------------------------------------------------

REMOTE_MARKERS = frozenset(
    {"remote", "work from home", "wfh", "virtual", "anywhere", "distributed"}
)

_REMOTE_RE = re.compile(
    r"\b(remote|work\s+from\s+home|wfh|virtual|anywhere|distributed)\b", re.IGNORECASE
)

_MULTI_SPLIT_RE = re.compile(r"\s*(?:\||;|/|\band\b)\s*", re.IGNORECASE)

#: Country names, ISO alpha-3 codes and common aliases → ISO-3166 alpha-2.
#: Curated rather than pulled from a package: the operator's search geography is
#: known, and a dependency that resolves "Bharat" is not one.
COUNTRY_ALIASES: dict[str, str] = {
    "india": "IN",
    "bharat": "IN",
    "ind": "IN",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "us": "US",
    "america": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "u.k.": "GB",
    "great britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "gbr": "GB",
    "ireland": "IE",
    "irl": "IE",
    "canada": "CA",
    "can": "CA",
    "germany": "DE",
    "deutschland": "DE",
    "deu": "DE",
    "ger": "DE",
    "france": "FR",
    "fra": "FR",
    "netherlands": "NL",
    "the netherlands": "NL",
    "holland": "NL",
    "nld": "NL",
    "spain": "ES",
    "espana": "ES",
    "esp": "ES",
    "portugal": "PT",
    "prt": "PT",
    "italy": "IT",
    "ita": "IT",
    "switzerland": "CH",
    "che": "CH",
    "austria": "AT",
    "aut": "AT",
    "belgium": "BE",
    "bel": "BE",
    "sweden": "SE",
    "swe": "SE",
    "norway": "NO",
    "nor": "NO",
    "denmark": "DK",
    "dnk": "DK",
    "finland": "FI",
    "fin": "FI",
    "poland": "PL",
    "pol": "PL",
    "czechia": "CZ",
    "czech republic": "CZ",
    "cze": "CZ",
    "romania": "RO",
    "rou": "RO",
    "israel": "IL",
    "isr": "IL",
    "united arab emirates": "AE",
    "uae": "AE",
    "are": "AE",
    "saudi arabia": "SA",
    "sau": "SA",
    "singapore": "SG",
    "sgp": "SG",
    "japan": "JP",
    "jpn": "JP",
    "china": "CN",
    "chn": "CN",
    "hong kong": "HK",
    "hkg": "HK",
    "taiwan": "TW",
    "twn": "TW",
    "south korea": "KR",
    "korea": "KR",
    "kor": "KR",
    "australia": "AU",
    "aus": "AU",
    "new zealand": "NZ",
    "nzl": "NZ",
    "brazil": "BR",
    "brasil": "BR",
    "bra": "BR",
    "mexico": "MX",
    "mex": "MX",
    "argentina": "AR",
    "arg": "AR",
    "chile": "CL",
    "chl": "CL",
    "colombia": "CO",
    "col": "CO",
    "south africa": "ZA",
    "zaf": "ZA",
    "kenya": "KE",
    "ken": "KE",
    "nigeria": "NG",
    "nga": "NG",
    "egypt": "EG",
    "egy": "EG",
    "turkey": "TR",
    "turkiye": "TR",
    "tur": "TR",
    "indonesia": "ID",
    "idn": "ID",
    "malaysia": "MY",
    "mys": "MY",
    "philippines": "PH",
    "phl": "PH",
    "thailand": "TH",
    "tha": "TH",
    "vietnam": "VN",
    "viet nam": "VN",
    "vnm": "VN",
    "sri lanka": "LK",
    "lka": "LK",
    "bangladesh": "BD",
    "bgd": "BD",
    "pakistan": "PK",
    "pak": "PK",
    "nepal": "NP",
    "npl": "NP",
}

#: Every alpha-2 we are prepared to recognise on its own, so that a bare "IN"
#: or "DE" token resolves without colliding with a city abbreviation.
_ALPHA2 = set(COUNTRY_ALIASES.values())

#: City aliases → canonical city name. The canonical form is what is stored, so
#: location filters and the cross-source dedup key are stable.
CITY_ALIASES: dict[str, str] = {
    "bangalore": "Bengaluru",
    "bangalore urban": "Bengaluru",
    "bengaluru": "Bengaluru",
    "bombay": "Mumbai",
    "mumbai": "Mumbai",
    "navi mumbai": "Navi Mumbai",
    "gurgaon": "Gurugram",
    "gurugram": "Gurugram",
    "calcutta": "Kolkata",
    "kolkata": "Kolkata",
    "trivandrum": "Thiruvananthapuram",
    "thiruvananthapuram": "Thiruvananthapuram",
    "madras": "Chennai",
    "chennai": "Chennai",
    "poona": "Pune",
    "pune": "Pune",
    "hyderabad": "Hyderabad",
    "secunderabad": "Hyderabad",
    "new delhi": "New Delhi",
    "delhi": "Delhi",
    "noida": "Noida",
    "greater noida": "Noida",
    "ghaziabad": "Ghaziabad",
    "faridabad": "Faridabad",
    "ahmedabad": "Ahmedabad",
    "gandhinagar": "Gandhinagar",
    "surat": "Surat",
    "vadodara": "Vadodara",
    "baroda": "Vadodara",
    "indore": "Indore",
    "bhopal": "Bhopal",
    "jaipur": "Jaipur",
    "lucknow": "Lucknow",
    "kanpur": "Kanpur",
    "chandigarh": "Chandigarh",
    "mohali": "Mohali",
    "ludhiana": "Ludhiana",
    "amritsar": "Amritsar",
    "dehradun": "Dehradun",
    "nagpur": "Nagpur",
    "nashik": "Nashik",
    "aurangabad": "Aurangabad",
    "goa": "Goa",
    "panaji": "Goa",
    "kochi": "Kochi",
    "cochin": "Kochi",
    "ernakulam": "Kochi",
    "kozhikode": "Kozhikode",
    "calicut": "Kozhikode",
    "coimbatore": "Coimbatore",
    "madurai": "Madurai",
    "mysore": "Mysuru",
    "mysuru": "Mysuru",
    "mangalore": "Mangaluru",
    "mangaluru": "Mangaluru",
    "vijayawada": "Vijayawada",
    "visakhapatnam": "Visakhapatnam",
    "vizag": "Visakhapatnam",
    "bhubaneswar": "Bhubaneswar",
    "patna": "Patna",
    "ranchi": "Ranchi",
    "raipur": "Raipur",
    "guwahati": "Guwahati",
    "jodhpur": "Jodhpur",
    "udaipur": "Udaipur",
    "varanasi": "Varanasi",
    "tiruchirappalli": "Tiruchirappalli",
    "trichy": "Tiruchirappalli",
    "salem": "Salem",
    "hubli": "Hubballi",
    "hubballi": "Hubballi",
    "belgaum": "Belagavi",
    "belagavi": "Belagavi",
    "thane": "Thane",
    "pimpri-chinchwad": "Pune",
    # --- global tech hubs ---
    "san francisco": "San Francisco",
    "sf": "San Francisco",
    "south san francisco": "San Francisco",
    "san jose": "San Jose",
    "sunnyvale": "Sunnyvale",
    "mountain view": "Mountain View",
    "palo alto": "Palo Alto",
    "santa clara": "Santa Clara",
    "cupertino": "Cupertino",
    "menlo park": "Menlo Park",
    "redwood city": "Redwood City",
    "oakland": "Oakland",
    "los angeles": "Los Angeles",
    "san diego": "San Diego",
    "seattle": "Seattle",
    "bellevue": "Bellevue",
    "redmond": "Redmond",
    "portland": "Portland",
    "denver": "Denver",
    "boulder": "Boulder",
    "austin": "Austin",
    "dallas": "Dallas",
    "houston": "Houston",
    "chicago": "Chicago",
    "new york": "New York",
    "new york city": "New York",
    "nyc": "New York",
    "brooklyn": "New York",
    "boston": "Boston",
    "cambridge": "Cambridge",
    "atlanta": "Atlanta",
    "miami": "Miami",
    "washington": "Washington, D.C.",
    "washington dc": "Washington, D.C.",
    "arlington": "Arlington",
    "raleigh": "Raleigh",
    "pittsburgh": "Pittsburgh",
    "detroit": "Detroit",
    "minneapolis": "Minneapolis",
    "phoenix": "Phoenix",
    "salt lake city": "Salt Lake City",
    "toronto": "Toronto",
    "vancouver": "Vancouver",
    "montreal": "Montreal",
    "ottawa": "Ottawa",
    "waterloo": "Waterloo",
    "london": "London",
    "manchester": "Manchester",
    "edinburgh": "Edinburgh",
    "bristol": "Bristol",
    "dublin": "Dublin",
    "cork": "Cork",
    "berlin": "Berlin",
    "munich": "Munich",
    "muenchen": "Munich",
    "hamburg": "Hamburg",
    "frankfurt": "Frankfurt",
    "cologne": "Cologne",
    "stuttgart": "Stuttgart",
    "paris": "Paris",
    "lyon": "Lyon",
    "toulouse": "Toulouse",
    "amsterdam": "Amsterdam",
    "rotterdam": "Rotterdam",
    "utrecht": "Utrecht",
    "eindhoven": "Eindhoven",
    "brussels": "Brussels",
    "zurich": "Zurich",
    "zuerich": "Zurich",
    "geneva": "Geneva",
    "vienna": "Vienna",
    "wien": "Vienna",
    "stockholm": "Stockholm",
    "gothenburg": "Gothenburg",
    "oslo": "Oslo",
    "copenhagen": "Copenhagen",
    "helsinki": "Helsinki",
    "warsaw": "Warsaw",
    "krakow": "Krakow",
    "wroclaw": "Wroclaw",
    "prague": "Prague",
    "praha": "Prague",
    "bucharest": "Bucharest",
    "budapest": "Budapest",
    "lisbon": "Lisbon",
    "porto": "Porto",
    "madrid": "Madrid",
    "barcelona": "Barcelona",
    "milan": "Milan",
    "rome": "Rome",
    "tel aviv": "Tel Aviv",
    "tel aviv-yafo": "Tel Aviv",
    "haifa": "Haifa",
    "dubai": "Dubai",
    "abu dhabi": "Abu Dhabi",
    "riyadh": "Riyadh",
    "singapore": "Singapore",
    "tokyo": "Tokyo",
    "osaka": "Osaka",
    "seoul": "Seoul",
    "beijing": "Beijing",
    "shanghai": "Shanghai",
    "shenzhen": "Shenzhen",
    "hong kong": "Hong Kong",
    "taipei": "Taipei",
    "sydney": "Sydney",
    "melbourne": "Melbourne",
    "brisbane": "Brisbane",
    "auckland": "Auckland",
    "wellington": "Wellington",
    "sao paulo": "Sao Paulo",
    "rio de janeiro": "Rio de Janeiro",
    "mexico city": "Mexico City",
    "buenos aires": "Buenos Aires",
    "santiago": "Santiago",
    "bogota": "Bogota",
    "cape town": "Cape Town",
    "johannesburg": "Johannesburg",
    "nairobi": "Nairobi",
    "lagos": "Lagos",
    "cairo": "Cairo",
    "istanbul": "Istanbul",
    "jakarta": "Jakarta",
    "kuala lumpur": "Kuala Lumpur",
    "manila": "Manila",
    "bangkok": "Bangkok",
    "ho chi minh city": "Ho Chi Minh City",
    "hanoi": "Hanoi",
    "colombo": "Colombo",
    "dhaka": "Dhaka",
    "karachi": "Karachi",
    "lahore": "Lahore",
    "islamabad": "Islamabad",
}

_AMAZON_PREFIX_RE = re.compile(r"^([A-Z]{2})\s*,\s*(.+)$")


@dataclass(frozen=True, slots=True)
class ParsedLocation:
    """The outcome of parsing one free-text location string.

    Attributes:
        raw: The input, verbatim. Always preserved.
        city: Canonical city name, or ``None`` when unresolvable.
        country: ISO-3166 alpha-2, or ``None`` when unresolvable.
        is_remote: True when a remote marker was present.
        all_locations: Every location the string listed, first one canonical.
    """

    raw: str | None
    city: str | None
    country: str | None
    is_remote: bool
    all_locations: tuple[str, ...]


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _key(value: str) -> str:
    cleaned = _strip_accents(value).lower().strip()
    cleaned = re.sub(r"[.’']", lambda m: "." if m.group(0) == "." else "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" ,-")


def _resolve_country(token: str) -> str | None:
    key = _key(token)
    if key in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[key]
    upper = token.strip().upper()
    if len(upper) == 2 and upper in _ALPHA2:
        return upper
    return None


def _resolve_city(token: str) -> str | None:
    return CITY_ALIASES.get(_key(token))


def _looks_like_location(fragment: str) -> bool:
    for part in (p.strip() for p in fragment.split(",")):
        if part and (_resolve_city(part) or _resolve_country(part)):
            return True
    return False


def parse_location(raw: str | None) -> ParsedLocation:
    """Parse a free-text location into a canonical city, country and remote flag.

    The pipeline, in order: split multi-location strings; detect and strip
    remote markers; resolve the country from the rightmost token (or the
    leftmost, for Amazon's ``"IN, KA, Bengaluru"`` shape); resolve the city
    against the curated table. Unresolvable input leaves both nulls and keeps
    ``raw`` verbatim — ``"Multiple Locations"`` is exactly this case, and it is
    never guessed.

    Args:
        raw: The upstream location string, or ``None``.

    Returns:
        A ``ParsedLocation``.
    """
    if raw is None or not raw.strip():
        return ParsedLocation(raw=raw, city=None, country=None, is_remote=False, all_locations=())

    text = unicodedata.normalize("NFKC", raw).replace(_NBSP, " ").strip()

    fragments = [f.strip() for f in _MULTI_SPLIT_RE.split(text) if f.strip()]
    candidates = [f for f in fragments if _looks_like_location(f)]
    if len(fragments) > 1 and len(candidates) >= 2:
        all_locations = tuple(fragments)
        primary = candidates[0]
    else:
        all_locations = (text,) if text else ()
        primary = text

    is_remote = bool(_REMOTE_RE.search(text))
    remainder = _REMOTE_RE.sub(" ", primary)
    remainder = re.sub(r"\s*[-–—]\s*", ", ", remainder)
    remainder = re.sub(r"\s+", " ", remainder).strip(" ,-")

    if not remainder:
        return ParsedLocation(
            raw=raw, city=None, country=None, is_remote=is_remote, all_locations=all_locations
        )

    amazon = _AMAZON_PREFIX_RE.match(remainder)
    if amazon and _resolve_country(amazon.group(1)):
        # "IN, KA, Bengaluru" — country first, city last. Reverse and continue.
        tail = [p.strip() for p in amazon.group(2).split(",") if p.strip()]
        parts = [*reversed(tail), amazon.group(1)]
    else:
        parts = [p.strip() for p in remainder.split(",") if p.strip()]

    country: str | None = None
    for index in range(len(parts) - 1, -1, -1):
        resolved = _resolve_country(parts[index])
        if resolved is not None:
            country = resolved
            parts = parts[:index] + parts[index + 1 :]
            break

    city: str | None = None
    for part in parts:
        resolved_city = _resolve_city(part)
        if resolved_city is not None:
            city = resolved_city
            break

    return ParsedLocation(
        raw=raw, city=city, country=country, is_remote=is_remote, all_locations=all_locations
    )


# ---------------------------------------------------------------------------
# §9.3 Seniority
# ---------------------------------------------------------------------------

#: Ordered. The order is load-bearing: "Senior Engineering Manager" must resolve
#: to `manager`, not `senior`, so manager patterns are tested before seniority
#: adjectives. "Lead" alone is deliberately absent — it means an IC track at
#: some employers and a people-management track at others, and a wrong guess
#: here feeds the stage-④ filter.
SENIORITY_PATTERNS: tuple[tuple[re.Pattern[str], Seniority], ...] = (
    (re.compile(r"\b(intern|internship|trainee|apprentice)\b", re.I), "intern"),
    (re.compile(r"\b(vp|vice president|chief|cxo|c[te]o|head of)\b", re.I), "executive"),
    (re.compile(r"\bdirector\b", re.I), "director"),
    (re.compile(r"\b(manager|mgr|lead engineering manager)\b", re.I), "manager"),
    (re.compile(r"\b(principal|distinguished|fellow)\b", re.I), "principal"),
    (re.compile(r"\bstaff\b|\bsde\s*(iii|3)\b|\bl[67]\b", re.I), "staff"),
    (re.compile(r"\b(senior|sr\.?|snr)\b|\bsde\s*(ii|2)\b|\bii\b", re.I), "senior"),
    (
        re.compile(r"\b(junior|jr\.?|associate|graduate|entry|campus|university)\b|\bi\b", re.I),
        "entry",
    ),
)

#: Where the source states seniority directly (SmartRecruiters
#: `experienceLevel`, Workable `experience`, Google `job_level`) that value wins
#: over title inference.
STATED_SENIORITY: dict[str, Seniority] = {
    "internship": "intern",
    "intern": "intern",
    "student": "intern",
    "entry_level": "entry",
    "entry level": "entry",
    "entry": "entry",
    "graduate": "entry",
    "junior": "entry",
    "associate": "entry",
    "mid": "mid",
    "mid_level": "mid",
    "mid-level": "mid",
    "experienced": "mid",
    "mid_senior_level": "senior",
    "mid-senior level": "senior",
    "senior": "senior",
    "staff": "staff",
    "principal": "principal",
    "manager": "manager",
    "management": "manager",
    "director": "director",
    "executive": "executive",
}


def infer_seniority(title: str | None) -> Seniority:
    """Infer a seniority band from a job title.

    A cheap deterministic hint used by the stage-④ filter, not a scoring input:
    scoring reads requirements, not titles. Ambiguity resolves to ``mid``, the
    permissive value, because a wrong high band would drop a role before a human
    ever saw it.

    Args:
        title: The posting title.

    Returns:
        The first matching band, or ``mid``.
    """
    if not title:
        return "unknown"
    for pattern, result in SENIORITY_PATTERNS:
        if pattern.search(title):
            return result
    return "mid"


def map_stated_seniority(value: str | None) -> Seniority | None:
    """Map an upstream seniority field to our vocabulary.

    Args:
        value: The upstream label or ID.

    Returns:
        The mapped band, or ``None`` when the value is unknown so the caller can
        fall back to title inference.
    """
    if not value:
        return None
    return STATED_SENIORITY.get(re.sub(r"\s+", " ", value.strip().lower()))


# ---------------------------------------------------------------------------
# §9.4 Employment type
# ---------------------------------------------------------------------------

EMPLOYMENT_TYPES: dict[str, EmploymentType] = {
    "fulltime": "full_time",
    "permanent": "full_time",
    "regular": "full_time",
    "parttime": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "fixedterm": "contract",
    "temporarycontract": "contract",
    "intern": "internship",
    "internship": "internship",
    "apprenticeship": "internship",
    "temporary": "temporary",
    "temp": "temporary",
    "seasonal": "temporary",
}

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def map_employment_type(value: str | None) -> EmploymentType:
    """Map an upstream employment-type string to the canonical value.

    Case-, punctuation- and whitespace-insensitive, so ``"Full time"``,
    ``"Full-Time"``, ``"FullTime"`` and ``"FULL_TIME"`` all land on
    ``full_time``.

    Args:
        value: The upstream string.

    Returns:
        The canonical value, or ``unknown``. An unmatched value is worth a log
        line at the call site so a new vendor vocabulary shows up rather than
        silently becoming ``unknown``.
    """
    if not value:
        return "unknown"
    key = _NON_ALNUM_RE.sub("", value.strip().lower())
    return EMPLOYMENT_TYPES.get(key, "unknown")


# ---------------------------------------------------------------------------
# §9.5 posted_at
# ---------------------------------------------------------------------------

#: Epoch-millisecond sanity window. A Lever `createdAt` read as seconds dates
#: every posting to 1970 and recency ranking silently inverts; this refuses the
#: result rather than storing it.
MIN_PLAUSIBLE_YEAR = 2000
MAX_PLAUSIBLE_YEAR = 2100

_RELATIVE_TODAY_RE = re.compile(r"\bposted\s+today\b", re.I)
_RELATIVE_YESTERDAY_RE = re.compile(r"\bposted\s+yesterday\b", re.I)
_RELATIVE_PLUS_RE = re.compile(r"\bposted\s+(\d+)\+\s*days?\s+ago\b", re.I)
_RELATIVE_DAYS_RE = re.compile(r"\bposted\s+(\d+)\s*days?\s+ago\b", re.I)


def _plausible(moment: datetime) -> datetime | None:
    if MIN_PLAUSIBLE_YEAR <= moment.year <= MAX_PLAUSIBLE_YEAR:
        return moment
    return None


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp and convert it to UTC.

    Args:
        value: An ISO-8601 string, with or without a ``Z`` suffix.

    Returns:
        A tz-aware UTC datetime, or ``None`` when unparseable or implausible.
        A timestamp with no offset is read as UTC.
    """
    if not value:
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _plausible(parsed.astimezone(UTC))


def parse_epoch_millis(value: int | float | str | None) -> datetime | None:
    """Parse epoch milliseconds (Lever's ``createdAt``).

    Args:
        value: Milliseconds since the epoch.

    Returns:
        A tz-aware UTC datetime, or ``None`` when the result falls outside
        2000–2100 — which is what a seconds/milliseconds mix-up looks like.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        millis = float(value)
    except (TypeError, ValueError):
        return None
    try:
        moment = datetime.fromtimestamp(millis / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return _plausible(moment)


def parse_date_only(value: str | None) -> datetime | None:
    """Parse a bare ``YYYY-MM-DD`` date as 00:00 UTC.

    A deliberate small backdating. It is uniform, so recency ordering is
    unaffected.

    Args:
        value: A bare date string.

    Returns:
        Midnight UTC on that date, or ``None``.
    """
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None
    return _plausible(datetime.combine(parsed, time.min, tzinfo=UTC))


def parse_relative_posted_on(phrase: str | None, run_start: datetime) -> datetime | None:
    """Resolve a Workday-style relative phrase against the run start.

    Against the run start, not ``now()``, so every posting in a run is dated on
    the same clock. ``"Posted 30+ Days Ago"`` returns ``None``: it means "at
    least 30 days", an unbounded lower bound, and storing it as exactly 30 would
    make a nine-month-old requisition look three weeks old.

    Args:
        phrase: The upstream phrase, e.g. ``"Posted 3 Days Ago"``.
        run_start: The run's start instant, tz-aware.

    Returns:
        A tz-aware UTC datetime, or ``None``.
    """
    if not phrase:
        return None
    anchor = run_start.astimezone(UTC)
    midnight = anchor.replace(hour=0, minute=0, second=0, microsecond=0)

    if _RELATIVE_PLUS_RE.search(phrase):
        return None
    if _RELATIVE_TODAY_RE.search(phrase):
        return midnight
    if _RELATIVE_YESTERDAY_RE.search(phrase):
        return midnight - timedelta(days=1)
    days_match = _RELATIVE_DAYS_RE.search(phrase)
    if days_match:
        return midnight - timedelta(days=int(days_match.group(1)))
    return None


def parse_posted_at(
    value: object, *, run_start: datetime, prefer: str | None = None
) -> datetime | None:
    """Resolve any of the shapes upstreams use for a posting date.

    Never defaults to ``now()``: ``job_posting.first_seen_at`` already records
    when we saw it, and fabricating a posting date would poison the one recency
    signal the ranker has.

    Args:
        value: An ISO timestamp, a bare date, epoch milliseconds, or a relative
            phrase.
        run_start: The run's start instant, used for relative phrases.
        prefer: Force one interpretation: ``"epoch_ms"``, ``"date"``,
            ``"iso"`` or ``"relative"``.

    Returns:
        A tz-aware UTC datetime, or ``None`` when nothing resolves.
    """
    if value is None:
        return None

    if prefer == "epoch_ms":
        return parse_epoch_millis(value if isinstance(value, int | float | str) else None)
    if prefer == "date":
        return parse_date_only(str(value))
    if prefer == "iso":
        return parse_iso_datetime(str(value))
    if prefer == "relative":
        return parse_relative_posted_on(str(value), run_start)

    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return _plausible(moment.astimezone(UTC))
    if isinstance(value, int | float) and not isinstance(value, bool):
        return parse_epoch_millis(value)

    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return parse_date_only(text)
    if re.fullmatch(r"\d{10,16}", text):
        return parse_epoch_millis(text)
    if "posted" in text.lower():
        return parse_relative_posted_on(text, run_start)
    return parse_iso_datetime(text)


# ---------------------------------------------------------------------------
# §9.6 Title normalisation (dedup key only)
# ---------------------------------------------------------------------------

_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}

TITLE_SYNONYMS: dict[str, str] = {
    "sde": "software engineer",
    "swe": "software engineer",
    "sw engineer": "software engineer",
    "ml": "machine learning",
    "ai/ml": "machine learning",
    "sr": "senior",
    "jr": "junior",
    "mgr": "manager",
}

_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)\s*$")
_TRAILING_QUALIFIER_RE = re.compile(
    r"\s*[-–—]\s*(remote|hybrid|on[- ]?site|contract|full[- ]?time|part[- ]?time"
    r"|[a-z .]*\b(india|usa|uk)\b|r?\d{4,})\s*$",
    re.I,
)


def normalise_title(title: str) -> str:
    """Canonicalise a title for the cross-source dedup key.

    ``job_posting.title`` stores the original; this is only the key. Aggressive
    normalisation is acceptable here because a false merge across sources is
    cheap to spot and a missed merge shows the operator the same role twice.

    Args:
        title: The posting title.

    Returns:
        A lower-cased, punctuation-collapsed key.
    """
    text = unicodedata.normalize("NFKC", title).replace(_NBSP, " ").strip().lower()

    previous = None
    while previous != text:
        previous = text
        text = _PARENTHETICAL_RE.sub("", text).strip()
        text = _TRAILING_QUALIFIER_RE.sub("", text).strip()

    text = re.sub(r"[^\w\s/+#.]+", " ", text)
    tokens = [t for t in re.split(r"[\s/]+", text) if t]
    tokens = [t.rstrip(".") or t for t in tokens]
    tokens = [_ROMAN.get(t, t) for t in tokens]
    tokens = [TITLE_SYNONYMS.get(t, t) for t in tokens]
    joined = " ".join(tokens)
    for phrase, replacement in TITLE_SYNONYMS.items():
        if " " in phrase:
            joined = joined.replace(phrase, replacement)
    return re.sub(r"\s+", " ", joined).strip()


__all__ = [
    "BLOCK",
    "CITY_ALIASES",
    "COUNTRY_ALIASES",
    "DROP",
    "EMPLOYMENT_TYPES",
    "REMOTE_MARKERS",
    "SENIORITY_PATTERNS",
    "TITLE_SYNONYMS",
    "ParsedLocation",
    "html_to_text",
    "infer_seniority",
    "map_employment_type",
    "map_stated_seniority",
    "normalise_title",
    "parse_date_only",
    "parse_epoch_millis",
    "parse_iso_datetime",
    "parse_location",
    "parse_posted_at",
    "parse_relative_posted_on",
]
