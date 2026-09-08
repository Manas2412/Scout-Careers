"""The ledger seeds only what the operator has approved.

`claim.evidence_ref` is NOT NULL because a claim nobody can trace is what the
table exists to refuse. The seed file ships every row as a draft read off résumé
bullets, so the gate lives one level up: `verdict` decides, and `no` is the
default — silence has to mean refusal, or an unreviewed number ends up behind a
match score and inside a document sent to an employer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scout_careers.cli.seed import (
    ATTESTED_PREFIX,
    DEFAULT_CLAIMS_PATH,
    load_claims,
)

BASE = """
claims:
  - key: pqbot.tests
    statement: 312 backend tests green.
    metric_value: "312"
    metric_unit: tests
    project: PQ-Bot
    verdict: {verdict}
    confidentiality: internal
    tags: [testing]
    verified_at: 2026-09-07
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "claims.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def claims_file(tmp_path: Path, verdict: str = "yes", **extra: str) -> Path:
    body = BASE.format(verdict=verdict)
    for key, value in extra.items():
        body += f"    {key}: {value}\n"
    return write(tmp_path, body)


# --------------------------------------------------------------------------
# The verdict, and what YAML does to it
# --------------------------------------------------------------------------


def test_an_unquoted_yes_approves(tmp_path: Path) -> None:
    """The Norway problem, and the reason for the `before` validator.

    YAML 1.1 resolves bare `yes` and `no` to booleans. The file and its
    instructions both say to write `verdict: yes`, so rejecting the boolean
    would refuse exactly the input this workflow asks for — and it would fail as
    an approval turning into an error rather than into a rejection.
    """
    seed = load_claims(claims_file(tmp_path, "yes"))
    assert seed.claims[0].verdict == "yes"
    assert seed.claims[0].approved is True


def test_an_unquoted_no_rejects(tmp_path: Path) -> None:
    seed = load_claims(claims_file(tmp_path, "no"))
    assert seed.claims[0].verdict == "no"
    assert seed.claims[0].approved is False


def test_a_quoted_verdict_works_too(tmp_path: Path) -> None:
    assert load_claims(claims_file(tmp_path, '"yes"')).claims[0].approved is True


def test_a_missing_verdict_defaults_to_rejected(tmp_path: Path) -> None:
    """Silence means refusal. A row nobody has looked at must never be seeded —
    that is the whole reason the default is not `yes`."""
    body = BASE.format(verdict="no").replace("    verdict: no\n", "")
    assert load_claims(write(tmp_path, body)).claims[0].approved is False


def test_an_unrecognised_verdict_is_refused(tmp_path: Path) -> None:
    """`maybe` is not a decision the ledger can act on."""
    with pytest.raises(Exception, match="verdict"):
        load_claims(claims_file(tmp_path, "maybe"))


# --------------------------------------------------------------------------
# Evidence references, when supplied
# --------------------------------------------------------------------------


def test_an_approved_claim_needs_no_reference(tmp_path: Path) -> None:
    """A bare `yes` is the operator's own attestation, and that is allowed.

    A gate people route around is worse than one they use: requiring a citation
    on all 53 rows would produce 53 citations of the least useful kind.
    """
    claim = load_claims(claims_file(tmp_path, "yes")).claims[0]
    assert claim.resolved_evidence().startswith(ATTESTED_PREFIX)
    assert "2026-09-07" in claim.resolved_evidence()


def test_a_supplied_reference_is_kept_verbatim(tmp_path: Path) -> None:
    reference = '"CI run #412 on main, 2026-09-01"'
    claim = load_claims(claims_file(tmp_path, "yes", evidence_ref=reference)).claims[0]
    assert claim.resolved_evidence() == "CI run #412 on main, 2026-09-01"


@pytest.mark.parametrize("placeholder", ["TODO", "tbd", "N/A", '"?"', '"-"', "FIXME", "pending"])
def test_a_placeholder_reference_is_refused_on_an_approved_claim(
    tmp_path: Path, placeholder: str
) -> None:
    """The guard cannot verify a reference is true — nothing here can. What it
    stops is the hurried version of getting past the gate, which is the failure
    that actually happens."""
    with pytest.raises(ValueError, match="references nothing"):
        load_claims(claims_file(tmp_path, "yes", evidence_ref=placeholder))


def test_a_reference_too_short_to_be_one_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="references nothing"):
        load_claims(claims_file(tmp_path, "yes", evidence_ref='"prod"'))


def test_a_rejected_claim_is_not_checked(tmp_path: Path) -> None:
    """It is not going anywhere, so its evidence_ref is nobody's problem —
    and failing the whole file over a row already marked `no` would punish the
    operator for the very act of rejecting it."""
    seed = load_claims(claims_file(tmp_path, "no", evidence_ref="TODO"))
    assert seed.claims[0].approved is False


# --------------------------------------------------------------------------
# File-level
# --------------------------------------------------------------------------


def test_a_duplicate_key_is_refused(tmp_path: Path) -> None:
    """Keys are cited from `resume_variant.content`. Two rows under one key
    means a bullet's citation resolves by file order, invisibly."""
    body = BASE.format(verdict="yes")
    body += BASE.format(verdict="yes").split("claims:", 1)[1]
    with pytest.raises(ValueError, match=re.escape("duplicate claim key")):
        load_claims(write(tmp_path, body))


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    """`extra="forbid"`: a seed file is edited by hand more than anything else
    here, and a typo'd key that is silently ignored costs an afternoon."""
    body = BASE.format(verdict="yes").replace("    tags: [testing]", "    tag: [testing]")
    with pytest.raises(Exception, match="tag"):
        load_claims(write(tmp_path, body))


def test_the_shipped_ledger_loads() -> None:
    """The real file parses and passes every rule.

    Replaces `test_the_shipped_draft_approves_nothing_yet`, which held while the
    file was unreviewed and — as its docstring promised — failed the moment rows
    were approved. That test's job is done: the drafts did not go live unnoticed.
    """
    if not DEFAULT_CLAIMS_PATH.exists():
        pytest.skip("no shipped claims file")
    seed = load_claims(DEFAULT_CLAIMS_PATH)
    assert seed.claims, "the ledger should not be empty"


def test_every_approved_claim_can_be_traced() -> None:
    """`claim.evidence_ref` is NOT NULL, so nothing may reach the table without
    one — an attestation counts, an empty string does not.

    Deliberately no count assertion. Approving or rejecting a claim is a
    judgement the operator revisits, and a test that broke on every review pass
    would be edited without being read.
    """
    if not DEFAULT_CLAIMS_PATH.exists():
        pytest.skip("no shipped claims file")
    for claim in load_claims(DEFAULT_CLAIMS_PATH).claims:
        if claim.approved:
            assert claim.resolved_evidence().strip(), f"{claim.key} would insert an empty ref"


def test_no_approved_claim_slipped_through_as_a_yaml_boolean() -> None:
    """The Norway problem, checked against the real file rather than a fixture.

    An unquoted `yes` arrives as `True`, and the normaliser turns it back into
    the string. If that ever regressed, the file would silently approve nothing —
    a failure that looks like a careful operator rather than a bug.
    """
    if not DEFAULT_CLAIMS_PATH.exists():
        pytest.skip("no shipped claims file")
    verdicts = {claim.verdict for claim in load_claims(DEFAULT_CLAIMS_PATH).claims}
    assert verdicts <= {"yes", "no"}
    assert "yes" in verdicts, "the ledger has been reviewed; some claim should be approved"
