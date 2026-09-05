"""The shipped seed file is valid before anything touches a database.

``scout-careers seed companies`` validates each row's adapter config and tags up
front, then writes all 44 rows in one transaction. Both halves of that matter and
both are tested here, because the failure mode is quiet: a rule enforced only
inside the write loop turns a typo in YAML into a stack trace from four frames
deep, after most of the file has already been created and rolled back.

That is not hypothetical. Tags were validated only by ``create_company``, and a
bare ``reserved`` on the last row failed on row 44.
"""

from __future__ import annotations

import pytest

from scout_careers.cli.seed import DEFAULT_SEED_PATH, load_seed
from scout_careers.ingest.resolve import UNMATCHED_TAGS
from scout_careers.registry.service import TagInvalid, validate_tags
from scout_careers.sources.registry import get_adapter


@pytest.fixture(scope="module")
def seed():
    return load_seed(DEFAULT_SEED_PATH)


def test_the_shipped_seed_file_parses(seed) -> None:
    assert seed.companies, "the seed file is empty"


def test_every_row_has_a_config_its_adapter_accepts(seed) -> None:
    for entry in seed.companies:
        get_adapter(entry.adapter).parse_config(entry.config)


def test_every_row_has_tags_the_registry_accepts(seed) -> None:
    """The check that would have caught the bare `reserved` tag in the file."""
    for entry in seed.companies:
        validate_tags(entry.tags)


def test_slugs_are_unique(seed) -> None:
    slugs = [entry.slug for entry in seed.companies]
    assert len(slugs) == len(set(slugs))


def test_a_bare_tag_is_refused_so_the_check_above_can_fail() -> None:
    # A test that only ever passes proves nothing about the validator it calls.
    with pytest.raises(TagInvalid):
        validate_tags(["reserved"])


def test_the_reserved_unmatched_row_s_tags_are_valid() -> None:
    """`ingest/resolve.py` writes that row directly, bypassing `create_company`.

    So the validator never sees it at runtime, and this is the only thing
    standing between an edit to ``UNMATCHED_TAGS`` and an invalid tag in the
    column.
    """
    assert validate_tags(list(UNMATCHED_TAGS)) == sorted(UNMATCHED_TAGS)
