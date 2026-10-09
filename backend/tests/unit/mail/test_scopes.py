"""The OAuth grant, and the properties that keep it minimal.

Scopes are the one part of a stored credential that can stop being sufficient
without anything about the credential changing — the *build* changes underneath
it. These tests hold both halves of that: that the set stays as small as the
work requires, and that an old token's insufficiency is detected somewhere an
operator will see it.
"""

from __future__ import annotations

from scout_careers.mail.scopes import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
    PHASE_1_SCOPES,
    PHASE_2_SCOPES,
    REQUIRED_SCOPES,
    missing_scopes,
)

# --------------------------------------------------------------------------
# What the grant may contain
# --------------------------------------------------------------------------


def test_no_write_scope_over_existing_mail_is_ever_requested() -> None:
    """The invariant the whole module exists to hold (EMAIL_INGESTION.md §2.4).

    `gmail.modify` and `gmail.compose` are write access to the operator's
    primary correspondence. `gmail.send` is not: it can *only* send, so it
    cannot label, archive, mark read, draft or delete. That asymmetry is what
    makes "the loop mutated my real mail" structurally impossible rather than
    merely unlikely, and it survives the digest.
    """
    forbidden = {
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://mail.google.com/",
    }
    assert not forbidden & set(REQUIRED_SCOPES)


def test_the_requested_set_is_exactly_read_and_send() -> None:
    """A scope list grows by accident — someone needs one API call, adds the
    scope that covers it, and the grant silently widens. This is the assertion
    that makes widening it a visible decision."""
    assert set(REQUIRED_SCOPES) == {GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE}


def test_required_scopes_is_the_name_every_caller_uses() -> None:
    """`REQUIRED_SCOPES` is the indirection that keeps the consent screen, the
    stored token and `auth status` from drifting apart across a phase change."""
    assert REQUIRED_SCOPES == PHASE_2_SCOPES


# --------------------------------------------------------------------------
# Detecting a token that predates the build
# --------------------------------------------------------------------------


def test_a_phase_1_token_is_reported_as_missing_send() -> None:
    """The live case this was written for.

    The operator's stored token was granted when the system could only read.
    It is valid, it refreshes, and it cannot send — so without this the send
    scope's absence first surfaces as a 403 from inside the send path at 08:15,
    on a schedule, where nobody is watching.
    """
    assert missing_scopes(PHASE_1_SCOPES) == (GMAIL_SEND_SCOPE,)


def test_a_sufficient_token_reports_nothing_missing() -> None:
    assert missing_scopes(REQUIRED_SCOPES) == ()


def test_extra_scopes_do_not_count_as_missing() -> None:
    """Google may return a grant wider than the one asked for — a previous
    consent for the same client persists. That is not this function's business:
    it answers "can this token do the work", not "is this token minimal"."""
    assert missing_scopes([*REQUIRED_SCOPES, "https://example.test/other"]) == ()


def test_an_empty_grant_is_missing_everything() -> None:
    """A token whose scopes were never recorded must not read as sufficient.
    Defaulting an unknown to "fine" is how a capability check becomes decorative.
    """
    assert missing_scopes(()) == REQUIRED_SCOPES


def test_whitespace_around_a_scope_does_not_hide_it() -> None:
    """Scope strings arrive from a space-delimited wire field and get split by
    several libraries on the way here. A stray space must not make a held scope
    look absent and send the operator through a pointless re-consent."""
    assert missing_scopes([f" {GMAIL_READONLY_SCOPE} ", f"{GMAIL_SEND_SCOPE} "]) == ()
