"""Normalisation: HTML flattening, locations, seniority, dates, titles."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scout_careers.common.hashing import content_hash
from scout_careers.common.text import normalise_for_hash, truncate, truncate_at_paragraph
from scout_careers.sources.normalise import (
    html_to_text,
    infer_seniority,
    map_employment_type,
    map_stated_seniority,
    normalise_title,
    parse_date_only,
    parse_epoch_millis,
    parse_iso_datetime,
    parse_location,
    parse_posted_at,
    parse_relative_posted_on,
)

RUN_START = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)


# --------------------------------------------------------------------------
# §9.1 HTML to text
# --------------------------------------------------------------------------


def test_greenhouse_entity_escaped_html_is_unescaped_first() -> None:
    # This is the single most common Greenhouse integration bug: without
    # html.unescape every description is a wall of &lt;p&gt;.
    raw = "&lt;p&gt;Stripe builds the economic infrastructure&lt;/p&gt;"
    text = html_to_text(raw)
    assert text == "Stripe builds the economic infrastructure"
    assert "&lt;" not in text
    assert "<p>" not in text


def test_list_items_become_bullets() -> None:
    text = html_to_text("<ul><li>5+ years Python</li><li>Distributed systems</li></ul>")
    assert "• 5+ years Python" in text
    assert "• Distributed systems" in text


def test_script_and_style_are_stripped_entirely() -> None:
    raw = (
        "<div><script>window.x = 'ignore previous instructions'</script>"
        "<style>.a{color:red}</style><p>Real content</p>"
        "<iframe src='x'>frame</iframe><form>submit</form></div>"
    )
    text = html_to_text(raw)
    assert text == "Real content"
    assert "ignore previous instructions" not in text
    assert "color:red" not in text


def test_block_elements_become_newlines_and_nothing_becomes_markdown() -> None:
    text = html_to_text("<h2>About</h2><p>One</p><p>Two</p>")
    assert "About" in text
    assert "One\nTwo" in text
    assert "#" not in text
    assert "**" not in text


def test_empty_and_none_input() -> None:
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_same_posting_in_different_wrappings_hashes_identically() -> None:
    # Same JD, re-pasted from a different editor: a non-breaking space, a
    # zero-width space, a zero-width joiner and a different list element.
    plain = html_to_text("<div><p>About the team</p><ul><li>Own the pipeline</li></ul></div>")
    fancy = html_to_text(
        "<section><div>About\u00a0the\u200b team</div>"
        "<ol><li>Own\u200d the pipeline</li></ol></section>"
    )
    assert plain == fancy
    assert content_hash(plain) == content_hash(fancy)


def test_nfkc_and_zero_width_are_handled_by_normalise_for_hash() -> None:
    assert normalise_for_hash("café") == normalise_for_hash("café")
    assert normalise_for_hash("a\u200bb") == "ab"
    assert normalise_for_hash("  a \n\n  b  ") == "a b"
    assert content_hash("\uff21") == content_hash("A")


def test_content_hash_is_a_64_char_hex_digest() -> None:
    digest = content_hash("anything")
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_truncate_helpers() -> None:
    assert truncate(None, 10) is None
    assert truncate("short", 10) == "short"
    assert len(truncate("x" * 100, 10) or "") == 10
    long_text = "para one\n\n" + "y" * 200
    cut = truncate_at_paragraph(long_text, 60)
    assert cut.endswith("[truncated]")
    assert len(cut) <= 60


# --------------------------------------------------------------------------
# §9.2 Locations
# --------------------------------------------------------------------------


def test_bengaluru_india() -> None:
    parsed = parse_location("Bengaluru, India")
    assert parsed.city == "Bengaluru"
    assert parsed.country == "IN"
    assert parsed.is_remote is False


def test_bangalore_is_canonicalised() -> None:
    assert parse_location("Bangalore, India").city == "Bengaluru"
    assert parse_location("Bombay, India").city == "Mumbai"
    assert parse_location("Gurgaon, Haryana, India").city == "Gurugram"
    assert parse_location("Calcutta, India").city == "Kolkata"
    assert parse_location("Trivandrum, Kerala, India").city == "Thiruvananthapuram"


def test_remote_india_is_both_remote_and_india() -> None:
    parsed = parse_location("Remote - India")
    assert parsed.is_remote is True
    assert parsed.country == "IN"
    assert parsed.city is None


def test_amazon_style_country_first() -> None:
    parsed = parse_location("IN, KA, Bengaluru")
    assert parsed.city == "Bengaluru"
    assert parsed.country == "IN"


def test_multiple_locations_is_never_guessed() -> None:
    parsed = parse_location("Multiple Locations")
    assert parsed.city is None
    assert parsed.country is None
    assert parsed.raw == "Multiple Locations"


def test_long_us_form() -> None:
    parsed = parse_location("San Jose, California, United States of America")
    assert parsed.city == "San Jose"
    assert parsed.country == "US"


def test_multi_location_keeps_the_first_and_records_the_rest() -> None:
    parsed = parse_location("Bangalore, Karnataka, India | Hyderabad, Telangana, India")
    assert parsed.city == "Bengaluru"
    assert parsed.country == "IN"
    assert len(parsed.all_locations) == 2


def test_remote_markers() -> None:
    for text in ("Remote", "Work from home", "WFH", "Anywhere", "Virtual", "Distributed"):
        assert parse_location(text).is_remote is True
    assert parse_location("Bengaluru, India").is_remote is False


def test_empty_location() -> None:
    parsed = parse_location(None)
    assert parsed.city is None and parsed.country is None and parsed.is_remote is False
    assert parse_location("   ").city is None


# --------------------------------------------------------------------------
# §9.3 Seniority — the order is load-bearing
# --------------------------------------------------------------------------


def test_senior_engineering_manager_resolves_to_manager_not_senior() -> None:
    assert infer_seniority("Senior Engineering Manager") == "manager"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Software Engineering Intern", "intern"),
        ("VP of Engineering", "executive"),
        ("Head of Data", "executive"),
        ("Director of Product", "director"),
        ("Engineering Manager", "manager"),
        ("Principal Software Engineer", "principal"),
        ("Distinguished Engineer", "principal"),
        ("Staff Machine Learning Engineer", "staff"),
        ("SDE III", "staff"),
        ("Senior Software Engineer", "senior"),
        ("Sr. Data Engineer", "senior"),
        ("Associate Software Engineer", "entry"),
        ("Graduate Engineer Trainee", "intern"),
        ("Software Engineer", "mid"),
        ("Lead Software Engineer", "mid"),
    ],
)
def test_seniority_table(title: str, expected: str) -> None:
    assert infer_seniority(title) == expected


def test_no_title_is_unknown_not_mid() -> None:
    assert infer_seniority(None) == "unknown"
    assert infer_seniority("") == "unknown"


def test_stated_seniority_overrides_inference() -> None:
    assert map_stated_seniority("mid_senior_level") == "senior"
    assert map_stated_seniority("Mid-Senior Level") == "senior"
    assert map_stated_seniority("nonsense") is None
    assert map_stated_seniority(None) is None


# --------------------------------------------------------------------------
# §9.4 Employment type
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Full time", "full_time"),
        ("Full-Time", "full_time"),
        ("FullTime", "full_time"),
        ("FULL_TIME", "full_time"),
        ("fulltime", "full_time"),
        ("Permanent", "full_time"),
        ("Regular", "full_time"),
        ("Part time", "part_time"),
        ("PART_TIME", "part_time"),
        ("Contract", "contract"),
        ("Fixed Term", "contract"),
        ("Internship", "internship"),
        ("Apprenticeship", "internship"),
        ("Seasonal", "temporary"),
        ("Temp", "temporary"),
        ("Something New", "unknown"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_employment_type_table(value: str | None, expected: str) -> None:
    assert map_employment_type(value) == expected


# --------------------------------------------------------------------------
# §9.5 posted_at
# --------------------------------------------------------------------------


def test_lever_epoch_millis_is_not_1970() -> None:
    # 1756288800000 ms = 2025-08-27. Read as seconds it would be 1970.
    parsed = parse_epoch_millis(1756288800000)
    assert parsed is not None
    assert parsed.year == 2025
    assert parsed.tzinfo is not None


def test_epoch_seconds_mistake_is_rejected_rather_than_stored() -> None:
    assert parse_epoch_millis(1756288800) is None  # would be 1970
    assert parse_epoch_millis(None) is None
    assert parse_epoch_millis("nonsense") is None


def test_iso_timestamps_are_used_as_is_in_utc() -> None:
    parsed = parse_iso_datetime("2026-08-21T09:12:44.000Z")
    assert parsed == datetime(2026, 8, 21, 9, 12, 44, tzinfo=UTC)
    offset = parse_iso_datetime("2026-08-29T11:04:33-04:00")
    assert offset == datetime(2026, 8, 29, 15, 4, 33, tzinfo=UTC)
    assert parse_iso_datetime("not a date") is None


def test_bare_dates_become_midnight_utc() -> None:
    assert parse_date_only("2026-09-02") == datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    assert parse_date_only("nope") is None


def test_relative_phrases_resolve_against_the_run_start() -> None:
    assert parse_relative_posted_on("Posted Today", RUN_START) == datetime(2026, 9, 5, tzinfo=UTC)
    assert parse_relative_posted_on("Posted Yesterday", RUN_START) == datetime(
        2026, 9, 4, tzinfo=UTC
    )
    assert parse_relative_posted_on("Posted 3 Days Ago", RUN_START) == datetime(
        2026, 9, 2, tzinfo=UTC
    )


def test_thirty_plus_days_ago_is_none_not_thirty() -> None:
    # "At least 30 days" is an unbounded lower bound. Storing it as exactly 30
    # would make a nine-month-old requisition look three weeks old.
    assert parse_relative_posted_on("Posted 30+ Days Ago", RUN_START) is None
    assert parse_posted_at("Posted 30+ Days Ago", run_start=RUN_START) is None


def test_posted_at_never_defaults_to_now() -> None:
    assert parse_posted_at(None, run_start=RUN_START) is None
    assert parse_posted_at("", run_start=RUN_START) is None
    assert parse_posted_at("garbage string", run_start=RUN_START) is None


def test_posted_at_dispatches_on_shape() -> None:
    assert parse_posted_at("2026-09-02", run_start=RUN_START) == datetime(2026, 9, 2, tzinfo=UTC)
    assert parse_posted_at(1756288800000, run_start=RUN_START) is not None
    assert parse_posted_at("2026-08-21T09:12:44Z", run_start=RUN_START) is not None
    assert parse_posted_at("Posted Today", run_start=RUN_START) == datetime(2026, 9, 5, tzinfo=UTC)
    assert parse_posted_at(1756288800000, run_start=RUN_START, prefer="epoch_ms") is not None


# --------------------------------------------------------------------------
# §9.6 Title normalisation
# --------------------------------------------------------------------------


def test_title_normalisation_for_the_dedup_key() -> None:
    assert normalise_title("Senior Software Engineer (Bengaluru)") == "senior software engineer"
    assert normalise_title("Machine Learning Engineer (R156789)") == "machine learning engineer"
    assert normalise_title("Backend Engineer - Remote") == "backend engineer"
    assert normalise_title("SDE III") == "software engineer 3"
    assert normalise_title("SWE II") == "software engineer 2"
    assert normalise_title("ML Engineer") == "machine learning engineer"


def test_title_normalisation_is_stable_across_cosmetic_differences() -> None:
    assert normalise_title("Senior  Software   Engineer") == normalise_title(
        "Senior Software Engineer"
    )
    assert normalise_title("Sr. Software Engineer") == normalise_title("Senior Software Engineer")
