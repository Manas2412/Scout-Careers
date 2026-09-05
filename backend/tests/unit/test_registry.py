"""The adapter registry and its boot assertion."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from scout_careers.common.errors import AdapterConfigError
from scout_careers.common.types import AtsType
from scout_careers.sources.registry import (
    ADAPTER_MODULES,
    ADAPTERS,
    NO_ADAPTER_BY_DESIGN,
    PHASE_1_ADAPTERS,
    get_adapter,
    verify_registry,
)


def test_phase_1_roster_is_the_four_documented_adapters() -> None:
    assert (
        frozenset({AtsType.GREENHOUSE, AtsType.LEVER, AtsType.ASHBY, AtsType.MAIL_ALERT})
        == PHASE_1_ADAPTERS
    )


def test_every_enum_value_except_manual_has_a_module_path() -> None:
    expected = set(AtsType) - NO_ADAPTER_BY_DESIGN
    assert set(ADAPTER_MODULES) == expected


def test_manual_has_no_adapter_by_design() -> None:
    # A hand-entered posting arrives through POST /postings/import; the enum
    # value exists only so it has a source row and a stable identity.
    with pytest.raises(AdapterConfigError, match="by design"):
        get_adapter(AtsType.MANUAL)


def test_verify_registry_passes_for_the_phase_1_roster() -> None:
    # All four Phase 1 adapters exist, satisfy the protocol structurally, and
    # declare the name they are registered under. A missing adapter is a boot
    # failure, not a runtime 500.
    verify_registry()
    assert set(ADAPTERS) >= PHASE_1_ADAPTERS


@pytest.mark.parametrize("ats", sorted(PHASE_1_ADAPTERS, key=lambda value: value.value))
def test_each_phase_1_adapter_resolves_and_declares_its_identity(ats: AtsType) -> None:
    adapter_cls = get_adapter(ats)
    assert adapter_cls.name is ats
    assert issubclass(adapter_cls.config_model, BaseModel)
    assert 0 <= adapter_cls.fidelity_rank <= 100
    assert adapter_cls.default_poll_interval_minutes >= 1


def test_the_fidelity_ranks_match_the_documented_table() -> None:
    # §8. Rank is a ClassVar, not config: making it configurable would let a
    # per-source setting override an ordering that exists to protect data
    # quality. The 50-point gap to mail_alert is the point of the scale.
    ranks = {ats: get_adapter(ats).fidelity_rank for ats in PHASE_1_ADAPTERS}
    assert ranks == {
        AtsType.ASHBY: 90,
        AtsType.GREENHOUSE: 90,
        AtsType.LEVER: 88,
        AtsType.MAIL_ALERT: 20,
    }
    assert (
        min(r for a, r in ranks.items() if a is not AtsType.MAIL_ALERT) - ranks[AtsType.MAIL_ALERT]
        >= 50
    )


def test_verify_registry_still_names_an_adapter_that_does_not_exist() -> None:
    # The failure mode the assertion exists for, exercised against a type whose
    # module is Phase 2 and is therefore genuinely absent.
    with pytest.raises(AdapterConfigError) as excinfo:
        verify_registry(frozenset({AtsType.WORKDAY}))
    assert "no adapter registered for workday" in str(excinfo.value)


def test_verify_registry_over_an_empty_roster_passes() -> None:
    verify_registry(frozenset())
