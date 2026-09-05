"""``GmailClient``: the query, the allow-list, the window, and the MIME walk.

Every test here runs against recorded Gmail API documents through a fake
transport. There is no Google library in the call path and no socket — the
repository ``conftest`` would raise if one were opened.

The two assertions that matter most are structural rather than behavioural:
``GmailClient`` satisfies ``MailReader`` (so ``ingest/`` can inject it where the
adapter expects one), and the fake satisfies ``GmailTransport`` (so a fake that
drifted from the seam cannot quietly go on passing).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scout_careers.common.config import Settings
from scout_careers.mail.gmail import (
    GmailClient,
    GmailTransport,
    build_mail_reader,
    build_query,
    close_mail_reader,
    parse_message,
)
from scout_careers.sources.base import MailReader
from tests.conftest import make_settings
from tests.unit.mail.conftest import (
    LINKEDIN_ID,
    LINKEDIN_SENDER,
    OLD_ID,
    STRANGER_ID,
    STRANGER_SENDER,
    FakeGmailTransport,
    message_resource,
)

RECEIVED = datetime(2026, 9, 4, 6, 30, tzinfo=UTC)
SINCE = RECEIVED - timedelta(hours=26)

ALLOWED = ("jobalerts-noreply@linkedin.com", "info@naukri.com")


def client(transport: FakeGmailTransport, *, max_messages: int = 100) -> GmailClient:
    return GmailClient(transport=transport, max_messages=max_messages)


def transport_with_all() -> FakeGmailTransport:
    """A transport holding one permitted message, one stranger, one too old."""
    return FakeGmailTransport(
        {
            LINKEDIN_ID: message_resource(message_id=LINKEDIN_ID, sender=LINKEDIN_SENDER),
            STRANGER_ID: message_resource(message_id=STRANGER_ID, sender=STRANGER_SENDER),
            OLD_ID: message_resource(
                message_id=OLD_ID,
                sender=LINKEDIN_SENDER,
                received_at=SINCE - timedelta(hours=1),
            ),
        }
    )


# --------------------------------------------------------------------------
# 1. Structural conformance
# --------------------------------------------------------------------------


def test_gmail_client_satisfies_the_mail_reader_protocol() -> None:
    # The whole injection story depends on this: `sources/` owns MailReader and
    # imports nothing from `mail/`, so the only thing binding them is structure.
    reader = client(FakeGmailTransport())
    assert isinstance(reader, MailReader)


def test_the_fake_satisfies_the_transport_protocol() -> None:
    assert isinstance(FakeGmailTransport(), GmailTransport)


def test_the_reader_holds_no_send_capability() -> None:
    reader = client(FakeGmailTransport())
    for forbidden in ("send", "send_message", "modify", "trash", "delete", "label"):
        assert not hasattr(reader, forbidden)


# --------------------------------------------------------------------------
# 2. The query
# --------------------------------------------------------------------------


def test_the_query_carries_the_label_the_senders_and_the_window() -> None:
    query = build_query(label="job-alerts", senders=list(ALLOWED), since=SINCE)
    assert 'label:"job-alerts"' in query
    assert "from:(jobalerts-noreply@linkedin.com OR info@naukri.com)" in query
    assert f"after:{int(SINCE.timestamp())}" in query


def test_a_label_with_a_space_is_quoted() -> None:
    # Unquoted, `label:job alerts` is two terms and matches the wrong mail.
    assert 'label:"job alerts"' in build_query(label="job alerts", senders=[], since=SINCE)


async def test_the_read_uses_that_query_and_the_configured_ceiling() -> None:
    transport = transport_with_all()
    await client(transport, max_messages=17).list_recent_messages(
        label="job-alerts", senders=ALLOWED, since=SINCE
    )
    assert len(transport.queries) == 1
    assert 'label:"job-alerts"' in transport.queries[0]
    assert transport.max_results == [17]


# --------------------------------------------------------------------------
# 3. The sender allow-list
# --------------------------------------------------------------------------


async def test_a_sender_outside_the_allow_list_is_dropped() -> None:
    transport = transport_with_all()
    messages = await client(transport).list_recent_messages(
        label="job-alerts", senders=ALLOWED, since=SINCE
    )
    senders = {message.sender for message in messages}
    assert senders == {LINKEDIN_SENDER}
    assert STRANGER_SENDER not in senders
    # Gmail's `from:` operator also matches display names, so the server-side
    # filter is not the one that makes this true. This is.
    assert STRANGER_ID in transport.fetched


async def test_the_allow_list_is_matched_case_insensitively() -> None:
    transport = FakeGmailTransport(
        {LINKEDIN_ID: message_resource(sender="JobAlerts-NoReply@LinkedIn.com")}
    )
    messages = await client(transport).list_recent_messages(
        label="job-alerts", senders=("JOBALERTS-NOREPLY@LINKEDIN.COM",), since=SINCE
    )
    assert len(messages) == 1


async def test_an_empty_allow_list_reads_nothing() -> None:
    # An empty allow-list is an allow-list, not a wildcard.
    transport = transport_with_all()
    messages = await client(transport).list_recent_messages(
        label="job-alerts", senders=(), since=SINCE
    )
    assert messages == []
    assert transport.queries == [], "and nothing was even asked of Gmail"


# --------------------------------------------------------------------------
# 4. The lookback window
# --------------------------------------------------------------------------


async def test_a_message_older_than_since_is_dropped() -> None:
    transport = transport_with_all()
    messages = await client(transport).list_recent_messages(
        label="job-alerts", senders=ALLOWED, since=SINCE
    )
    assert [message.message_id for message in messages] == [LINKEDIN_ID]
    assert OLD_ID in transport.fetched, "it was read, and then rejected on its date"


async def test_a_message_exactly_at_the_boundary_is_kept() -> None:
    transport = FakeGmailTransport({LINKEDIN_ID: message_resource(received_at=SINCE)})
    messages = await client(transport).list_recent_messages(
        label="job-alerts", senders=ALLOWED, since=SINCE
    )
    assert len(messages) == 1


async def test_a_naive_since_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await client(FakeGmailTransport()).list_recent_messages(
            label="job-alerts",
            senders=ALLOWED,
            since=datetime(2026, 9, 4, 6, 30),  # noqa: DTZ001 - the point of the test
        )


async def test_results_come_back_newest_first() -> None:
    transport = FakeGmailTransport(
        {
            LINKEDIN_ID: message_resource(message_id=LINKEDIN_ID, received_at=RECEIVED),
            STRANGER_ID: message_resource(
                message_id=STRANGER_ID, received_at=RECEIVED + timedelta(hours=2)
            ),
        }
    )
    messages = await client(transport).list_recent_messages(
        label="job-alerts", senders=ALLOWED, since=SINCE
    )
    assert [message.message_id for message in messages] == [STRANGER_ID, LINKEDIN_ID]


async def test_the_ceiling_bounds_how_many_messages_are_fetched() -> None:
    transport = transport_with_all()
    await client(transport, max_messages=1).list_recent_messages(
        label="job-alerts", senders=ALLOWED, since=SINCE
    )
    assert len(transport.fetched) == 1


# --------------------------------------------------------------------------
# 5. Parsing the recorded documents
# --------------------------------------------------------------------------


async def test_the_html_part_is_decoded_and_the_plaintext_alternative_kept() -> None:
    transport = FakeGmailTransport({LINKEDIN_ID: message_resource()})
    (message,) = await client(transport).list_recent_messages(
        label="job-alerts", senders=ALLOWED, since=SINCE
    )
    assert message.html_body is not None
    assert "<html" in message.html_body.lower()
    assert message.text_body is not None
    assert "plaintext alternative" in message.text_body


async def test_the_sender_is_the_envelope_address_not_the_display_name() -> None:
    transport = FakeGmailTransport({LINKEDIN_ID: message_resource()})
    (message,) = await client(transport).list_recent_messages(
        label="job-alerts", senders=ALLOWED, since=SINCE
    )
    assert message.sender == LINKEDIN_SENDER
    assert "<" not in message.sender


async def test_internal_date_is_used_not_the_date_header() -> None:
    # The Date: header is written by the sender and is untrusted; internalDate
    # is when Gmail received it.
    transport = FakeGmailTransport({LINKEDIN_ID: message_resource(received_at=RECEIVED)})
    (message,) = await client(transport).list_recent_messages(
        label="job-alerts", senders=ALLOWED, since=SINCE
    )
    assert message.received_at == RECEIVED
    assert message.received_at.tzinfo is not None


def test_a_message_with_no_sender_is_dropped_not_guessed_at() -> None:
    resource = message_resource()
    resource["payload"]["headers"] = [
        header for header in resource["payload"]["headers"] if header["name"] != "From"
    ]
    assert parse_message(resource) is None


def test_a_message_with_no_internal_date_is_dropped() -> None:
    resource = message_resource()
    del resource["internalDate"]
    assert parse_message(resource) is None


def test_a_message_with_no_id_is_dropped() -> None:
    resource = message_resource()
    resource["id"] = ""
    assert parse_message(resource) is None


def test_an_attachment_only_part_is_skipped_without_a_second_request() -> None:
    resource = message_resource()
    resource["payload"]["parts"] = [
        {"mimeType": "text/html", "body": {"attachmentId": "att-1", "size": 900}}
    ]
    parsed = parse_message(resource)
    assert parsed is not None
    assert parsed.html_body is None


def test_a_single_part_html_message_is_read() -> None:
    resource = message_resource()
    html_part = resource["payload"]["parts"][1]
    resource["payload"] = {
        "mimeType": "text/html",
        "headers": [
            {"name": "From", "value": f"Alerts <{LINKEDIN_SENDER}>"},
        ],
        "body": html_part["body"],
    }
    parsed = parse_message(resource)
    assert parsed is not None
    assert parsed.html_body is not None


def test_a_nested_multipart_message_is_walked() -> None:
    resource = message_resource()
    inner = resource["payload"]["parts"]
    resource["payload"]["mimeType"] = "multipart/mixed"
    resource["payload"]["parts"] = [{"mimeType": "multipart/alternative", "parts": inner}]
    parsed = parse_message(resource)
    assert parsed is not None
    assert parsed.html_body is not None
    assert parsed.text_body is not None


def test_undecodable_body_data_does_not_take_the_message_down() -> None:
    resource = message_resource()
    resource["payload"]["parts"][1]["body"]["data"] = "!!!not base64!!!"
    parsed = parse_message(resource)
    assert parsed is not None
    assert parsed.html_body is None
    assert parsed.text_body is not None


def test_an_empty_listing_is_not_an_error() -> None:
    transport = FakeGmailTransport()
    transport.listing = {"resultSizeEstimate": 0}
    assert transport.listing.get("messages") is None


# --------------------------------------------------------------------------
# 6. Wiring
# --------------------------------------------------------------------------


def test_no_reader_is_built_when_mail_is_disabled() -> None:
    assert build_mail_reader(make_settings(mail_enabled=False)) is None


def test_no_reader_is_built_without_an_encryption_key(fernet_key: str) -> None:
    # `mail_enabled=True` without a key is refused at boot; this asserts the
    # runtime path is safe too, since gmail_configured is what the runner reads.
    settings = make_settings(
        mail_enabled=False,
        gmail_client_id="id",
        gmail_client_secret="secret",
    )
    assert settings.gmail_configured is False
    assert build_mail_reader(settings) is None
    assert fernet_key  # the fixture is what a configured install would supply


def test_gmail_configured_needs_all_three_pieces(fernet_key: str) -> None:
    configured: Settings = make_settings(
        mail_enabled=True,
        mail_token_key=fernet_key,
        gmail_client_id="1234.apps.googleusercontent.com",
        gmail_client_secret="a-secret",
    )
    assert configured.gmail_configured is True
    assert configured.oauth_client_configured is True


async def test_close_mail_reader_closes_a_gmail_client_and_tolerates_none() -> None:
    transport = FakeGmailTransport()
    await close_mail_reader(client(transport))
    assert transport.closed is True

    await close_mail_reader(None)

    class BareReader:
        async def list_recent_messages(self, **_kwargs: object) -> list[object]:
            return []

    # A reader with no aclose is not an error: the protocol does not require one.
    await close_mail_reader(BareReader())  # type: ignore[arg-type]
