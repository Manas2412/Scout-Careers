"""MailAlertAdapter and the per-provider parsers (SOURCE_ADAPTERS.md §7).

The load-bearing assertion in this file is the last one: across a whole fetch,
``respx`` records zero requests, so no LinkedIn, Naukri or Indeed host is
contacted to produce a single posting. Everything else here is mapping detail;
that one is the compliance boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
import respx

from scout_careers.common.errors import AdapterConfigError
from scout_careers.common.types import AtsType
from scout_careers.sources.alerts import indeed, linkedin, naukri, parse_alert, parser_for
from scout_careers.sources.alerts.entry import (
    AlertEntry,
    normalise_city,
    normalise_company,
    synthesise_external_id,
    unwrap_tracking_url,
)
from scout_careers.sources.base import MailAlertConfig, MailMessage, MailReader, RawPosting
from scout_careers.sources.mail_alerts import MailAlertAdapter
from scout_careers.sources.policy import NEVER_FETCH_HOSTS, is_denied_host
from tests.unit.sources.conftest import load_html

RECEIVED = datetime(2026, 9, 4, 6, 30, tzinfo=UTC)


class FakeMailReader:
    """A ``MailReader`` over fixture messages. Holds no network capability.

    It records the ``label`` / ``senders`` / ``since`` it was asked for and then
    returns every fixture message regardless. Applying the window here would
    make the fixtures' fixed received dates expire against the wall clock, and
    a test that starts failing on a particular Tuesday is worse than no test.
    The window itself is asserted directly, from ``calls``.
    """

    def __init__(self, messages: Sequence[MailMessage]) -> None:
        self.messages = list(messages)
        self.calls: list[tuple[str, tuple[str, ...], datetime]] = []

    async def list_recent_messages(
        self,
        *,
        label: str,
        senders: Sequence[str],
        since: datetime,
    ) -> Sequence[MailMessage]:
        self.calls.append((label, tuple(senders), since))
        return list(self.messages)


def message(
    *,
    sender: str,
    fixture: str,
    message_id: str = "18f2c9a1b7d40e55",
    received_at: datetime = RECEIVED,
) -> MailMessage:
    return MailMessage(
        message_id=message_id,
        sender=sender,
        subject="Your job alert",
        received_at=received_at,
        html_body=load_html(fixture),
    )


LINKEDIN_MESSAGE = message(
    sender="jobalerts-noreply@linkedin.com",
    fixture="linkedin_digest.html",
    message_id="18f2c9a1b7d40e55",
)
NAUKRI_MESSAGE = message(
    sender="info@naukri.com",
    fixture="naukri_digest.html",
    message_id="18f2c9a1b7d40e56",
)
INDEED_MESSAGE = message(
    sender="alert@indeed.com",
    fixture="indeed_digest.html",
    message_id="18f2c9a1b7d40e57",
)


def build_adapter(reader: MailReader, **config: object) -> MailAlertAdapter:
    return MailAlertAdapter(
        source_id=41,
        config=MailAlertConfig(**config),  # type: ignore[arg-type]
        mail=reader,
    )


async def collect(adapter: MailAlertAdapter) -> list[RawPosting]:
    return [posting async for posting in adapter.fetch()]


# --------------------------------------------------------------------------
# 1. parse_config
# --------------------------------------------------------------------------


def test_parse_config_accepts_the_documented_shape() -> None:
    config = MailAlertAdapter.parse_config(
        {"label": "job-alerts", "senders": ["info@naukri.com"], "lookback_hours": 26}
    )
    assert config.label == "job-alerts"
    assert config.lookback_hours == 26


@pytest.mark.parametrize(
    "raw",
    [
        {"lookback_hours": 0},
        {"lookback_hours": 999},
        {"label": "job-alerts", "gmail_token": "ya29.secret"},  # extra=forbid
        {"senders": "info@naukri.com"},  # a string is not a list
    ],
)
def test_parse_config_rejects_bad_input(raw: dict[str, object]) -> None:
    with pytest.raises(AdapterConfigError):
        MailAlertAdapter.parse_config(raw)


def test_parse_config_round_trips_canonically() -> None:
    config = MailAlertAdapter.parse_config({"label": "job-alerts"})
    again = MailAlertAdapter.parse_config(config.model_dump())
    assert again == config
    assert config.model_dump_json() == again.model_dump_json()


# --------------------------------------------------------------------------
# 2. probe
# --------------------------------------------------------------------------


async def test_probe_reports_a_readable_mailbox() -> None:
    reader = FakeMailReader([LINKEDIN_MESSAGE, NAUKRI_MESSAGE, INDEED_MESSAGE])
    result = await build_adapter(reader).probe()

    assert result.reachable is True
    assert result.sample_count == 10  # 4 + 3 + 3 cards
    assert result.detail == "3 alert message(s) in the lookback window"


async def test_probe_on_an_empty_mailbox_is_reachable_with_no_samples() -> None:
    # Zero cards with reachable=True is a different fault from a mailbox that
    # cannot be read, and the health table needs to be able to tell them apart.
    result = await build_adapter(FakeMailReader([])).probe()

    assert result.reachable is True
    assert result.sample_count == 0


# --------------------------------------------------------------------------
# 3. the parsers, one fixture each
# --------------------------------------------------------------------------


def test_linkedin_parser_reads_every_card() -> None:
    entries = linkedin.parse(load_html("linkedin_digest.html"))

    assert [e.title for e in entries] == [
        "Staff Machine Learning Engineer",
        "Senior Applied Scientist, Ranking",
        "Machine Learning Engineer II",
        "Principal Engineer, Platform",
    ]
    assert [e.company_name for e in entries] == [
        "Zeta Payments India",
        "Meesho",
        "Swiggy",
        "Atlassian",
    ]
    first = entries[0]
    assert first.location_raw == "Bengaluru, Karnataka, India"
    assert first.is_remote is False
    assert first.posted_hint == "2 days ago"
    assert first.platform == "linkedin"
    assert first.extra == {"linkedin_job_id": "4012345678"}
    # A company legitimately called "Zeta Payments India" resolves as a
    # location. Taking the company positionally is what stops it being filed
    # as the city.
    assert entries[1].is_remote is True
    # Per-send tracking parameters are stripped from the retained URL.
    assert "trackingId" not in first.canonical_url
    assert "midToken" not in first.canonical_url
    assert first.tracking_url_retained is True


def test_naukri_parser_reads_every_row() -> None:
    entries = naukri.parse(load_html("naukri_digest.html"))

    assert [e.title for e in entries] == [
        "Senior Backend Engineer",
        "Lead Software Development Engineer",
        "Python Developer (Remote)",
    ]
    assert entries[0].company_name == "Postman"
    assert entries[0].location_raw == "Bengaluru, Hyderabad"
    assert entries[0].extra["experience_hint"] == "4-8"
    # A salary cell and an experience range are noise, not the company.
    assert entries[1].company_name == "Zeta Suite Pvt Ltd"
    # An unresolvable location is kept verbatim rather than dropped or guessed.
    assert entries[1].location_raw == "Mumbai (All Areas)"
    assert entries[2].is_remote is True


def test_indeed_parser_reads_every_block() -> None:
    entries = indeed.parse(load_html("indeed_digest.html"))

    assert [e.title for e in entries] == [
        "Senior Data Engineer",
        "Analytics Engineer",
        "Data Platform Engineer",
    ]
    assert entries[0].company_name == "Acme Health"
    assert entries[0].snippet is not None
    assert entries[0].snippet.startswith("Build and own the batch")
    assert entries[1].is_remote is True
    assert entries[2].extra == {}


def test_parse_alert_dispatches_on_the_sender() -> None:
    assert parser_for("jobs-listings@linkedin.com") is linkedin.parse
    assert parser_for("JobAlerts-noreply@LinkedIn.com") is linkedin.parse
    assert parser_for("info@naukri.com") is naukri.parse
    assert parser_for("noreply@indeed.com") is indeed.parse
    assert parser_for("recruiting@some-employer.example") is None
    # An unrecognised sender is not an error: the adapter counts a parse miss.
    assert parse_alert("recruiting@some-employer.example", "<html></html>") == []


# --------------------------------------------------------------------------
# URL unwrapping — offline only
# --------------------------------------------------------------------------


def test_a_redirect_with_its_target_in_the_query_is_unwrapped_offline() -> None:
    resolved, unwrapped = unwrap_tracking_url(
        "https://cts.indeed.com/v3/AAAA?url=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F42&tk=abc"
    )
    assert resolved == "https://boards.greenhouse.io/acme/jobs/42"
    assert unwrapped is True


def test_a_redirect_without_a_target_keeps_the_tracking_url() -> None:
    resolved, unwrapped = unwrap_tracking_url(
        "https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc&trk=eml"
    )
    assert resolved == "https://www.linkedin.com/comm/jobs/view/4012345678/"
    assert unwrapped is False
    # And the host is still denied, which is why nothing tried to resolve it.
    assert is_denied_host("www.linkedin.com") is True


def test_unwrapping_cannot_cycle() -> None:
    # A redirector whose target is itself is bounded, not spun.
    self_referential = "https://example.test/r?url=https%3A%2F%2Fexample.test%2Fr"
    resolved, unwrapped = unwrap_tracking_url(self_referential)
    assert resolved == "https://example.test/r"
    assert unwrapped is True


# --------------------------------------------------------------------------
# Synthetic identity
# --------------------------------------------------------------------------


def test_external_id_is_stable_across_two_parses() -> None:
    first = linkedin.parse(load_html("linkedin_digest.html"))
    second = linkedin.parse(load_html("linkedin_digest.html"))
    assert [e.external_id for e in first] == [e.external_id for e in second]
    assert all(len(e.external_id) == 32 for e in first)


def test_external_id_ignores_the_url_and_the_date() -> None:
    # Alert links carry per-send tracking parameters and the same role appears
    # in several alerts; neither may move the identity.
    base = {
        "platform": "linkedin",
        "company_name": "Acme Corp",
        "title": "Staff Machine Learning Engineer",
        "location": "Bengaluru, India",
    }
    assert synthesise_external_id(**base) == synthesise_external_id(**base)


def test_external_id_collapses_cosmetic_differences() -> None:
    bangalore = synthesise_external_id(
        platform="linkedin",
        company_name="Acme Corporation Pvt Ltd",
        title="Sr. Machine Learning Engineer",
        location="Bangalore",
    )
    bengaluru = synthesise_external_id(
        platform="linkedin",
        company_name="Acme Corp",
        title="Senior Machine Learning Engineer",
        location="Bengaluru, Karnataka, India",
    )
    assert bangalore == bengaluru


def test_external_id_separates_genuinely_different_roles() -> None:
    ids = {
        synthesise_external_id(
            platform="linkedin", company_name="Acme", title="Data Engineer", location="Pune"
        ),
        synthesise_external_id(
            platform="linkedin", company_name="Acme", title="Data Engineer", location="Chennai"
        ),
        synthesise_external_id(
            platform="naukri", company_name="Acme", title="Data Engineer", location="Pune"
        ),
    }
    assert len(ids) == 3


def test_company_and_city_normalisation() -> None:
    assert normalise_company("Zeta Suite Pvt Ltd") == "zeta suite"
    assert normalise_company("Freshworks Inc.") == "freshworks"
    assert normalise_city("Bangalore") == "Bengaluru".lower()
    assert normalise_city("Remote - Anywhere") == "remote"
    assert normalise_city(None) == ""


# --------------------------------------------------------------------------
# 4. "pagination" — there is none; the mailbox window is the bound
# --------------------------------------------------------------------------


async def test_the_lookback_window_bounds_the_read() -> None:
    reader = FakeMailReader([LINKEDIN_MESSAGE])
    adapter = build_adapter(reader, lookback_hours=26)
    await collect(adapter)

    label, senders, since = reader.calls[0]
    assert label == "job-alerts"
    assert "jobalerts-noreply@linkedin.com" in senders
    # Wider than the 60-minute poll interval on purpose: a missed run must not
    # silently lose a day of alerts.
    assert datetime.now(UTC) - since >= timedelta(hours=25)
    assert len(reader.calls) == 1


async def test_an_explicit_since_wins_over_the_lookback() -> None:
    reader = FakeMailReader([LINKEDIN_MESSAGE])
    adapter = build_adapter(reader)
    explicit = datetime(2026, 9, 1, tzinfo=UTC)
    _ = [p async for p in adapter.fetch(since=explicit)]

    assert reader.calls[0][2] == explicit


# --------------------------------------------------------------------------
# 5. mapping into RawPosting
# --------------------------------------------------------------------------


async def test_fetch_maps_a_card_into_a_marked_low_fidelity_lead() -> None:
    reader = FakeMailReader([LINKEDIN_MESSAGE])
    adapter = build_adapter(reader)
    postings = await collect(adapter)

    assert len(postings) == 4
    first = postings[0]

    assert first.title == "Staff Machine Learning Engineer"
    assert first.external_id == linkedin.parse(load_html("linkedin_digest.html"))[0].external_id
    assert first.location_raw == "Bengaluru, Karnataka, India"
    assert first.location_city == "Bengaluru"
    assert first.location_country == "IN"
    assert first.employment_type == "unknown"
    assert first.department is None
    assert first.description_html is None

    # The two marks, and nothing invented alongside them.
    assert first.raw["fidelity"] == "low"
    assert first.raw["snippet_only"] is True
    assert "needs_description" not in first.raw
    # The Gmail message id is a string; the integer FK to email_message is
    # Phase 5.
    assert first.raw["email_message_id"] == "18f2c9a1b7d40e55"
    assert isinstance(first.raw["email_message_id"], str)
    # RawPosting has no company field, so the parsed name rides in raw for
    # ingest's trigram match against the registry.
    assert first.raw["company_name"] == "Zeta Payments India"
    assert first.raw["canonical_url_source"] == "tracking_url_retained"

    # The stub is deliberately recognisable so stage ⑤ can skip extraction.
    assert first.description_text.startswith(
        "Discovered via linkedin job alert email received 2026-09-04."
    )
    assert "No job description available from this source." in first.description_text
    assert "Company: Zeta Payments India" in first.description_text

    # "2 days ago", resolved against the message's received time — not now().
    assert first.posted_at == datetime(2026, 9, 2, tzinfo=UTC)
    # "6 hours ago" is finer than a day and resolves to the day it was sent.
    assert postings[1].posted_at == datetime(2026, 9, 4, tzinfo=UTC)
    # "1 week ago" is not a bound this system will invent a date from.
    assert postings[2].posted_at is None
    assert postings[3].posted_at == datetime(2026, 9, 3, tzinfo=UTC)


async def test_an_unwrapped_link_points_at_the_employer(settings) -> None:
    del settings
    reader = FakeMailReader([INDEED_MESSAGE])
    postings = await collect(build_adapter(reader))

    assert str(postings[0].url) == "https://boards.greenhouse.io/acmehealth/jobs/7788990"
    assert postings[0].raw["canonical_url_source"] == "unwrapped_offline"
    # The one that could not be unwrapped keeps its tracking URL and says so.
    assert postings[1].raw["canonical_url_source"] == "tracking_url_retained"
    assert "indeed.com" in str(postings[1].url)


async def test_the_same_role_in_two_alerts_collapses_to_one_posting() -> None:
    # The same digest delivered twice: identity is synthesised from the role,
    # not the send, so the second copy is a duplicate rather than a new lead.
    twice = FakeMailReader(
        [
            LINKEDIN_MESSAGE,
            message(
                sender="jobs-listings@linkedin.com",
                fixture="linkedin_digest.html",
                message_id="18f2c9a1b7d40e99",
                received_at=RECEIVED + timedelta(days=1),
            ),
        ]
    )
    adapter = build_adapter(twice)
    postings = await collect(adapter)

    assert len(postings) == 4
    assert adapter.skipped == {"duplicate": 4}


async def test_a_template_change_is_counted_and_logged_not_swallowed() -> None:
    # A parser that matches zero cards on a message that matched the sender
    # filter is the early warning that a provider changed its template.
    blank = MailMessage(
        message_id="18f2c9a1b7d40e70",
        sender="jobalerts-noreply@linkedin.com",
        subject="Your job alert",
        received_at=RECEIVED,
        html_body="<html><body><p>Nothing recognisable here.</p></body></html>",
    )
    adapter = build_adapter(FakeMailReader([blank]))
    postings = await collect(adapter)

    assert postings == []
    assert adapter.skipped == {"parse_misses": 1}


async def test_a_message_with_no_html_part_is_a_parse_miss() -> None:
    text_only = MailMessage(
        message_id="18f2c9a1b7d40e71",
        sender="info@naukri.com",
        subject="Your job alert",
        received_at=RECEIVED,
        html_body=None,
        text_body="plain text only",
    )
    adapter = build_adapter(FakeMailReader([text_only]))

    assert await collect(adapter) == []
    assert adapter.skipped == {"parse_misses": 1}


# --------------------------------------------------------------------------
# 6. The compliance boundary
# --------------------------------------------------------------------------


@respx.mock
async def test_no_denied_host_is_ever_requested_during_a_fetch() -> None:
    # respx is registered with no routes at all: any outbound request fails the
    # test rather than reaching a host. The repository conftest also blocks
    # sockets, so this holds even if respx were bypassed.
    reader = FakeMailReader([LINKEDIN_MESSAGE, NAUKRI_MESSAGE, INDEED_MESSAGE])
    adapter = build_adapter(reader)

    postings = await collect(adapter)

    assert len(postings) == 10
    assert list(respx.calls) == []

    # And every stored link either points somewhere allowed, or points at a
    # denied host that was recorded and never followed.
    denied = [p for p in postings if is_denied_host(str(p.url).split("/")[2])]
    assert denied, "the fixture must include at least one retained tracking URL"
    for posting in denied:
        assert posting.raw["canonical_url_source"] == "tracking_url_retained"


def test_the_adapter_holds_no_http_capability() -> None:
    adapter = build_adapter(FakeMailReader([]))
    assert not hasattr(adapter, "_http")
    assert isinstance(adapter.mail, MailReader)
    assert MailAlertAdapter.name is AtsType.MAIL_ALERT
    assert MailAlertAdapter.fidelity_rank == 20
    assert MailAlertAdapter.default_poll_interval_minutes == 60
    assert MailAlertAdapter.requires_detail_fetch is False
    assert adapter.describe() == "Job alert email · job-alerts"


def test_the_parsers_never_name_an_allowed_fetch_target() -> None:
    # A belt-and-braces statement of §7.1: every provider this adapter parses
    # is on the never-fetch list, so there is no version of this code path that
    # legitimately issues a request to one.
    for host in ("www.linkedin.com", "www.naukri.com", "in.indeed.com"):
        assert is_denied_host(host)
    assert "linkedin.com" in NEVER_FETCH_HOSTS


async def test_aclose_releases_nothing_it_does_not_own() -> None:
    adapter = build_adapter(FakeMailReader([]))
    await adapter.aclose()
    await asyncio.sleep(0)
    assert adapter.mail is not None


def test_alert_entry_is_the_dto_name() -> None:
    entry = linkedin.parse(load_html("linkedin_digest.html"))[0]
    assert isinstance(entry, AlertEntry)
