"""Which company a mail-alert lead is filed under.

The pure half — grouping and the plan's use of an override — is tested here
offline. The trigram query itself needs Postgres and is covered by the
integration test; what these tests protect is the rule that a lead which cannot
be attributed still lands somewhere visible, and that two employers whose roles
share a title do not collapse into one posting because they shared a source.
"""

from __future__ import annotations

from scout_careers.common.hashing import content_hash
from scout_careers.common.types import AtsType
from scout_careers.ingest.dedup import DedupCandidate, plan_collapse
from scout_careers.ingest.persist import (
    UPDATE_COLUMNS,
    Change,
    ExistingPosting,
    classify,
    plan_persist,
)
from scout_careers.ingest.resolve import (
    ALERT_COMPANY_KEY,
    ALERT_RAW_FLAGS,
    NEEDS_DESCRIPTION_KEY,
    alert_company_names,
)
from tests.unit.ingest.conftest import RUN_START, make_posting

MAILBOX_COMPANY = 3


def _alert(external_id: str, company_name: str | None, **overrides):
    raw = dict(overrides.pop("raw", {}))
    if company_name is not None:
        raw[ALERT_COMPANY_KEY] = company_name
    return make_posting(external_id=external_id, raw=raw, **overrides)


def _plan(postings, *, existing=None, overrides=None, raw_extra=None):
    return plan_persist(
        postings,
        source_id=7,
        company_id=MAILBOX_COMPANY,
        existing=existing or {},
        now=RUN_START,
        max_description_chars=60_000,
        company_overrides=overrides,
        raw_extra=raw_extra,
        id_factory=lambda: "01JXXXXXXXXXXXXXXXXXXXXXXX",
    )


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------


def test_legal_suffixes_collapse_into_one_group() -> None:
    grouped = alert_company_names(
        [
            _alert("1", "Acme Corp"),
            _alert("2", "Acme Corporation Pvt Ltd"),
            _alert("3", "Globex"),
        ]
    )
    assert len(grouped) == 2
    acme = next(ids for name, ids in grouped.items() if "acme" in name)
    assert sorted(acme) == ["1", "2"]


def test_a_lead_with_no_parsed_employer_is_not_grouped() -> None:
    # It still has to be *placed*; that is `resolve_alert_companies`' job, and
    # it places it on `unmatched` precisely because it is absent here.
    assert alert_company_names([_alert("1", None), _alert("2", "   ")]) == {}


# --------------------------------------------------------------------------
# The override reaches the row
# --------------------------------------------------------------------------


def test_each_lead_is_filed_under_its_own_resolved_company() -> None:
    postings = [_alert("1", "Acme"), _alert("2", "Globex")]
    plan = _plan(postings, overrides={"1": 41, "2": 42})
    assert {row["external_id"]: row["company_id"] for row in plan.upserts} == {"1": 41, "2": 42}


def test_a_posting_absent_from_the_overrides_keeps_the_source_company() -> None:
    plan = _plan([_alert("1", "Acme")], overrides={})
    assert plan.upserts[0]["company_id"] == MAILBOX_COMPANY


def test_mail_sourced_rows_are_flagged_for_the_extractor_to_skip() -> None:
    plan = _plan([_alert("1", "Acme")], raw_extra=ALERT_RAW_FLAGS)
    assert plan.upserts[0]["raw"][NEEDS_DESCRIPTION_KEY] is True


def test_board_rows_carry_no_such_flag() -> None:
    plan = _plan([make_posting(external_id="1")])
    assert NEEDS_DESCRIPTION_KEY not in plan.upserts[0]["raw"]


# --------------------------------------------------------------------------
# Why this module exists at all
# --------------------------------------------------------------------------


MAILBOX_SOURCE = 52


def _lead(posting_id: str, company_id: int, content_hash: str) -> DedupCandidate:
    """One alert lead as the collapse decision sees it."""
    return DedupCandidate(
        id=posting_id,
        company_id=company_id,
        title="Software Engineer",
        location_city="Bengaluru",
        adapter=AtsType.MAIL_ALERT,
        source_id=MAILBOX_SOURCE,
        content_hash=content_hash,
        first_seen_at=RUN_START,
    )


def test_two_employers_in_one_digest_are_never_collapsed_into_one() -> None:
    """Two defences now cover this, and the second was added after the first.

    When this test was written, resolution was the only thing standing between
    two employers' identically-titled roles and a collapse: both leads carried
    the mailbox's own ``company_id``, and the dedup key is
    ``(company_id, normalised title, city)``.

    ``same_role`` has since been added for a different reason — 685 board
    postings collapsed by title alone — and it happens to cover this case too,
    because two leads from one mailbox are same-source and their stub text
    embeds the employer name, so the hashes differ. That overlap is worth
    stating rather than papering over: the original "reproduce the bug" half of
    this test can no longer fail, so asserting it would assert nothing.

    What is still true, and is what this checks, is that **both** layers hold:
    unresolved leads with different stubs do not collapse, and resolved leads
    are separated by company as well.
    """
    unresolved = [
        _lead("01LEADA", MAILBOX_COMPANY, "stub-acme"),
        _lead("01LEADB", MAILBOX_COMPANY, "stub-globex"),
    ]
    assert plan_collapse(unresolved).supersede == {}, "same_role holds on its own"

    resolved = [_lead("01LEADA", 41, "stub-acme"), _lead("01LEADB", 42, "stub-globex")]
    assert plan_collapse(resolved).supersede == {}, "resolution holds on its own"


def test_resolution_is_what_separates_two_leads_with_identical_text() -> None:
    """The case ``same_role`` cannot reach, so resolution is not redundant.

    Same source, same title, same city, same hash — ``same_role`` says collapse,
    and it is right to, because on that evidence alone they are one listing seen
    twice. Only the parsed employer name distinguishes them, and only resolution
    reads it. Without resolution the second lead disappears.
    """
    same_text = [
        _lead("01LEADA", MAILBOX_COMPANY, "identical"),
        _lead("01LEADB", MAILBOX_COMPANY, "identical"),
    ]
    assert plan_collapse(same_text).supersede == {"01LEADB": "01LEADA"}

    resolved = [_lead("01LEADA", 41, "identical"), _lead("01LEADB", 42, "identical")]
    assert plan_collapse(resolved).supersede == {}


# --------------------------------------------------------------------------
# Re-attachment when the employer is finally tracked
# --------------------------------------------------------------------------


def test_a_promoted_company_re_files_a_lead_whose_text_never_changed() -> None:
    """An alert lead's stub text is identical every run.

    So the only signal that it now belongs to a real company is the company id
    itself. If change detection looked at the hash alone, a lead parked on
    ``unmatched`` would stay there for ever and promoting the employer would
    silently do nothing.
    """
    description = "Discovered via linkedin job alert email received 2026-09-05."
    stored = ExistingPosting(
        id="01LEAD",
        content_hash=content_hash(description),
        company_id=99,  # `unmatched`
    )
    assert classify(stored, content_hash(description), company_id=99) is Change.UNCHANGED
    assert classify(stored, content_hash(description), company_id=41) is Change.UPDATED

    plan = _plan(
        [_alert("1", "Acme", description_text=description)],
        existing={"1": stored},
        overrides={"1": 41},
    )
    assert plan.touched_ids == []
    assert plan.counts.updated == 1
    assert plan.upserts[0]["id"] == "01LEAD"
    assert plan.upserts[0]["company_id"] == 41


def test_company_id_is_rewritten_on_conflict() -> None:
    """The re-file above is only real if the upsert actually writes the column."""
    assert "company_id" in UPDATE_COLUMNS


def test_a_board_posting_is_never_reclassified_by_a_company_it_cannot_move_to() -> None:
    stored = ExistingPosting(id="01A", content_hash="same", company_id=MAILBOX_COMPANY)
    # No overrides: `plan_persist` passes the source's own company, which is
    # where the row already is, so nothing is disturbed.
    plan = _plan([make_posting(external_id="1", description_text="x")], existing={"1": stored})
    assert plan.counts.updated == 1  # the hash moved, not the company
    assert classify(stored, "same", company_id=MAILBOX_COMPANY) is Change.UNCHANGED
