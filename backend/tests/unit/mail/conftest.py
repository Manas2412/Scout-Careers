"""Harness for the ``mail/`` tests. No Google, no network, no real mailbox.

The repository-level ``conftest`` already patches ``socket`` to raise, so a call
that escapes these fakes fails loudly. What is added here is a transport that
serves recorded Gmail API documents, and a Fernet key generated per run — a test
credential is never a literal (SECURITY_ARCHITECTURE.md §7.2 rule 2).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from scout_careers.mail.tokens import OAuthToken

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "mail"

#: The sender the recorded message is from.
LINKEDIN_SENDER = "jobalerts-noreply@linkedin.com"

#: A sender that is emphatically not on any allow-list under test.
STRANGER_SENDER = "deals@some-marketing-list.example"

LINKEDIN_ID = "18f2c9a1b7d40e55"
STRANGER_ID = "18f2c9a1b7d40e60"
OLD_ID = "18f2c9a1b7d40e61"


def load_fixture(name: str) -> Any:
    """Load a recorded Gmail API document.

    Args:
        name: File name under ``tests/fixtures/mail``.

    Returns:
        The decoded JSON.
    """
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def message_resource(
    *,
    message_id: str = LINKEDIN_ID,
    sender: str = LINKEDIN_SENDER,
    received_at: datetime | None = None,
) -> dict[str, Any]:
    """Return the recorded message, optionally re-attributed or re-dated.

    The body is always the real captured LinkedIn digest — the point of the
    fixture — while the id, the ``From`` header and ``internalDate`` are the
    three things a test needs to vary to exercise the allow-list and the window.

    Args:
        message_id: The Gmail id to report.
        sender: The envelope address, wrapped in a display name so the header
            parsing is exercised rather than bypassed.
        received_at: Overrides ``internalDate``.

    Returns:
        A deep copy, so a mutating test cannot poison the next one.
    """
    resource: dict[str, Any] = copy.deepcopy(load_fixture("gmail_message_linkedin.json"))
    resource["id"] = message_id
    resource["threadId"] = message_id
    for header in resource["payload"]["headers"]:
        if header["name"] == "From":
            header["value"] = f"Alerts <{sender}>"
    if received_at is not None:
        resource["internalDate"] = str(int(received_at.timestamp() * 1000))
    return resource


class FakeGmailTransport:
    """Serves recorded documents and records what it was asked for.

    Satisfies ``GmailTransport`` structurally, which is asserted rather than
    assumed: a fake that has drifted from the protocol proves nothing.
    """

    def __init__(self, resources: Mapping[str, dict[str, Any]] | None = None) -> None:
        self.resources: dict[str, dict[str, Any]] = dict(resources or {})
        self.listing: dict[str, Any] = load_fixture("gmail_messages_list.json")
        self.queries: list[str] = []
        self.max_results: list[int] = []
        self.fetched: list[str] = []
        self.closed = False

    async def list_messages(self, *, query: str, max_results: int) -> Mapping[str, Any]:
        self.queries.append(query)
        self.max_results.append(max_results)
        ids = set(self.resources)
        return {
            "messages": [
                entry for entry in self.listing["messages"] if entry["id"] in ids or not ids
            ],
            "resultSizeEstimate": len(ids),
        }

    async def get_message(self, message_id: str) -> Mapping[str, Any]:
        self.fetched.append(message_id)
        return self.resources[message_id]

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fernet_key() -> str:
    """A Fernet key generated for this test. Never a literal."""
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def secret_value() -> str:
    """A stand-in refresh token, generated per run so it cannot be committed."""
    return "refresh-" + Fernet.generate_key().decode("ascii")[:32]


@pytest.fixture
def token(secret_value: str) -> OAuthToken:
    """A credential carrying the generated secret."""
    return OAuthToken(
        refresh_token=SecretStr(secret_value),
        client_id="1234567890-abc.apps.googleusercontent.com",
        client_secret=SecretStr("client-" + secret_value),
        scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )
