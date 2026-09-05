"""Change detection: what counts as new, updated and unchanged.

These three counts are what the digest reports and what the operator reads to
decide whether a board is alive, so the rules that produce them are tested
against the plan rather than against the database.
"""

from __future__ import annotations

from scout_careers.common.hashing import content_hash
from scout_careers.ingest.persist import (
    TRUNCATION_FLAG,
    TRUNCATION_ORIGINAL_CHARS,
    UPDATE_COLUMNS,
    Change,
    ExistingPosting,
    bound_description,
    classify,
    plan_persist,
)
from scout_careers.sources.normalise import html_to_text
from tests.unit.ingest.conftest import RUN_START, make_posting

DESCRIPTION = "Build and run distributed services."


def _plan(postings, existing=None, *, limit: int = 60_000):
    return plan_persist(
        postings,
        source_id=7,
        company_id=3,
        existing=existing or {},
        now=RUN_START,
        max_description_chars=limit,
        id_factory=lambda: "01JXXXXXXXXXXXXXXXXXXXXXXX",
    )


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_unknown_identity_is_new() -> None:
    assert classify(None, "abc") is Change.NEW


def test_same_identity_with_a_moved_hash_is_updated() -> None:
    stored = ExistingPosting(id="01A", content_hash="old", company_id=3)
    assert classify(stored, "new") is Change.UPDATED


def test_same_identity_with_the_same_hash_is_unchanged() -> None:
    stored = ExistingPosting(id="01A", content_hash="same", company_id=3)
    assert classify(stored, "same") is Change.UNCHANGED


def test_plan_buckets_all_three_kinds() -> None:
    postings = [
        make_posting(external_id="1", description_text=DESCRIPTION),
        make_posting(external_id="2", description_text="A different role entirely."),
        make_posting(external_id="3", description_text="Brand new."),
    ]
    existing = {
        "1": ExistingPosting(id="01ONE", content_hash=content_hash(DESCRIPTION), company_id=3),
        "2": ExistingPosting(id="01TWO", content_hash=content_hash("stale text"), company_id=3),
    }
    plan = _plan(postings, existing)

    assert plan.counts.new == 1
    assert plan.counts.updated == 1
    assert plan.counts.unchanged == 1
    assert plan.counts.total == 3
    # The unchanged posting is only touched, never rewritten.
    assert plan.touched_ids == ["01ONE"]
    assert {row["external_id"] for row in plan.upserts} == {"2", "3"}


def test_updated_row_keeps_its_existing_ulid() -> None:
    existing = {"2": ExistingPosting(id="01TWO", content_hash="stale", company_id=3)}
    plan = _plan([make_posting(external_id="2")], existing)
    assert plan.upserts[0]["id"] == "01TWO"


def test_new_row_gets_a_fresh_ulid_and_this_run_s_timestamps() -> None:
    plan = _plan([make_posting(external_id="9")])
    row = plan.upserts[0]
    assert row["id"] == "01JXXXXXXXXXXXXXXXXXXXXXXX"
    assert row["first_seen_at"] == RUN_START
    assert row["last_seen_at"] == RUN_START


def test_every_seen_posting_resets_the_close_state() -> None:
    plan = _plan([make_posting(external_id="9")])
    row = plan.upserts[0]
    assert row["missed_runs"] == 0
    assert row["closed_at"] is None


# --------------------------------------------------------------------------
# The hash is stable across cosmetic change
# --------------------------------------------------------------------------


def test_same_posting_in_different_html_wrapping_keeps_the_same_hash() -> None:
    # The same words, re-wrapped by a recruiter editing in a different editor.
    first = html_to_text("<div><p>We are hiring.</p><p>Build things.</p></div>")
    second = html_to_text(
        "<section><div>We are hiring.</div>\n\n<div>Build things.</div></section>"
    )
    assert content_hash(first) == content_hash(second)

    stored = {"1": ExistingPosting(id="01ONE", content_hash=content_hash(first), company_id=3)}
    plan = _plan([make_posting(external_id="1", description_text=second)], stored)

    assert plan.counts.unchanged == 1
    assert plan.counts.updated == 0
    assert plan.upserts == []


def test_zero_width_characters_do_not_look_like_an_edit() -> None:
    stored = {
        "1": ExistingPosting(id="01ONE", content_hash=content_hash(DESCRIPTION), company_id=3)
    }
    pasted = DESCRIPTION.replace(" ", "​ ", 1)
    plan = _plan([make_posting(external_id="1", description_text=pasted)], stored)
    assert plan.counts.unchanged == 1


# --------------------------------------------------------------------------
# The §9.1 bound
# --------------------------------------------------------------------------


def test_truncation_is_marked_in_raw() -> None:
    long_text = "\n\n".join(
        f"Paragraph {index} of a very long job description." for index in range(40)
    )
    plan = _plan([make_posting(external_id="1", description_text=long_text)], limit=200)

    row = plan.upserts[0]
    assert len(row["description_text"]) <= 200
    assert row["description_text"].endswith("[truncated]")
    assert row["raw"][TRUNCATION_FLAG] is True
    assert row["raw"][TRUNCATION_ORIGINAL_CHARS] == len(long_text)


def test_a_description_inside_the_bound_is_not_marked() -> None:
    plan = _plan([make_posting(external_id="1", description_text=DESCRIPTION)])
    assert TRUNCATION_FLAG not in plan.upserts[0]["raw"]


def test_bound_description_reports_whether_it_cut() -> None:
    text, cut = bound_description("short", 1_000)
    assert (text, cut) == ("short", False)
    _, cut_long = bound_description("x" * 5_000, 100)
    assert cut_long is True


def test_the_hash_describes_the_text_that_is_stored() -> None:
    long_text = "\n\n".join(f"Paragraph {index}." for index in range(200))
    plan = _plan([make_posting(external_id="1", description_text=long_text)], limit=300)
    row = plan.upserts[0]
    assert row["content_hash"] == content_hash(row["description_text"])


def test_the_adapter_s_raw_dict_is_not_mutated() -> None:
    posting = make_posting(external_id="1", description_text="x" * 5_000, raw={"id": 1})
    _plan([posting], limit=1_000)
    assert posting.raw == {"id": 1}


# --------------------------------------------------------------------------
# What an upsert must not overwrite
# --------------------------------------------------------------------------


def test_first_seen_at_is_never_rewritten_on_conflict() -> None:
    # It records when *we* first saw the role and recency ranking keys on it.
    assert "first_seen_at" not in UPDATE_COLUMNS


def test_supersession_state_is_not_rewritten_on_conflict() -> None:
    # filtered_out belongs to ingest/dedup.py; persisting must not un-supersede.
    assert "filtered_out" not in UPDATE_COLUMNS
    assert "filter_reason" not in UPDATE_COLUMNS


def test_close_state_is_reset_on_conflict() -> None:
    assert "last_seen_at" in UPDATE_COLUMNS
    assert "missed_runs" in UPDATE_COLUMNS
    assert "closed_at" in UPDATE_COLUMNS


# --------------------------------------------------------------------------
# Defensive
# --------------------------------------------------------------------------


def test_a_duplicate_external_id_in_one_fetch_is_dropped() -> None:
    # ON CONFLICT refuses a statement that touches the same row twice, so the
    # duplicate is dropped here rather than failing the whole board.
    plan = _plan(
        [
            make_posting(external_id="1", description_text="First."),
            make_posting(external_id="1", description_text="Second."),
        ]
    )
    assert plan.counts.new == 1
    assert len(plan.upserts) == 1
    assert plan.upserts[0]["description_text"] == "First."


def test_an_empty_fetch_plans_nothing() -> None:
    plan = _plan([])
    assert plan.upserts == []
    assert plan.touched_ids == []
    assert plan.counts.total == 0
