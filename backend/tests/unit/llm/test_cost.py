"""Cost accounting, and the breaker.

Cost is computed from the ``usage`` block on every response, never estimated.
These tests exist because the breaker is the only thing standing between a
malformed prompt and an unbounded bill, and a breaker that has never been
observed to trip is a breaker nobody knows the state of.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scout_careers.llm.base import BudgetExhausted, Usage
from scout_careers.llm.cost import Prices, RunCost, cost_inr, prices_for
from tests.conftest import make_settings

# $3 / $15 per million, 88 INR to the dollar — the shipped `fast` figures.
SONNET = Prices(input_usd=Decimal("3.00"), output_usd=Decimal("15.00"))
INR = Decimal("88")


def tracker(**overrides) -> RunCost:
    return RunCost.from_settings(make_settings(**overrides))


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------


def test_a_typical_extraction_costs_what_it_should() -> None:
    """4,000 in and 600 out: (4000*3 + 600*15) / 1e6 * 88 = 1.848 -> 1.85."""
    amount = cost_inr(Usage(input_tokens=4_000, output_tokens=600), SONNET, inr_per_usd=INR)
    assert amount == Decimal("1.85")


def test_cost_is_quantised_to_paise() -> None:
    amount = cost_inr(Usage(input_tokens=1, output_tokens=1), SONNET, inr_per_usd=INR)
    assert amount.as_tuple().exponent == -2


def test_a_cached_read_is_cheaper_than_a_fresh_read() -> None:
    fresh = cost_inr(Usage(input_tokens=10_000, output_tokens=0), SONNET, inr_per_usd=INR)
    cached = cost_inr(
        Usage(input_tokens=0, output_tokens=0, cached_input_tokens=10_000),
        SONNET,
        inr_per_usd=INR,
    )
    assert cached < fresh
    assert cached == (fresh * Decimal("0.10")).quantize(Decimal("0.01"))


def test_writing_the_cache_costs_more_than_not_using_it() -> None:
    """The failure this pricing exists to expose.

    A cache written on every call and never read is a **cost increase**. If
    cache writes were modelled as free, that outcome would look identical to a
    working cache, and the first sign would be the invoice.
    """
    plain = cost_inr(Usage(input_tokens=10_000, output_tokens=0), SONNET, inr_per_usd=INR)
    written = cost_inr(
        Usage(input_tokens=0, output_tokens=0, cache_write_tokens=10_000),
        SONNET,
        inr_per_usd=INR,
    )
    assert written > plain


def test_caching_pays_back_after_one_reuse() -> None:
    """Write once at 1.25x, then read at 0.10x. Two calls beat two fresh ones."""
    fresh_two = cost_inr(Usage(input_tokens=20_000, output_tokens=0), SONNET, inr_per_usd=INR)
    write_then_read = cost_inr(
        Usage(input_tokens=0, output_tokens=0, cache_write_tokens=10_000), SONNET, inr_per_usd=INR
    ) + cost_inr(
        Usage(input_tokens=0, output_tokens=0, cached_input_tokens=10_000), SONNET, inr_per_usd=INR
    )
    assert write_then_read < fresh_two


def test_zero_usage_costs_nothing() -> None:
    assert cost_inr(Usage(0, 0), SONNET, inr_per_usd=INR) == Decimal("0.00")


def test_the_alias_decides_the_price_table() -> None:
    settings = make_settings(
        llm_price_fast_in=Decimal("3.00"),
        llm_price_fast_out=Decimal("15.00"),
        llm_price_strong_in=Decimal("15.00"),
        llm_price_strong_out=Decimal("75.00"),
    )
    assert prices_for(settings, "fast").input_usd == Decimal("3.00")
    assert prices_for(settings, "strong").output_usd == Decimal("75.00")


# --------------------------------------------------------------------------
# The breaker
# --------------------------------------------------------------------------


def test_an_untouched_tracker_is_closed_and_permits_calls() -> None:
    cost = tracker()
    assert cost.is_open is False
    cost.guard()  # does not raise


def test_the_breaker_opens_at_the_budget_and_refuses_further_calls() -> None:
    cost = tracker(llm_daily_budget_inr=Decimal("2.00"))
    cost.record(Usage(input_tokens=4_000, output_tokens=600), SONNET)  # 1.85
    assert cost.is_open is False
    cost.record(Usage(input_tokens=4_000, output_tokens=600), SONNET)  # 3.70
    assert cost.is_open is True

    with pytest.raises(BudgetExhausted) as caught:
        cost.guard()
    assert "left for the next run" in str(caught.value)


def test_the_guard_is_checked_before_a_call_not_after() -> None:
    """After is too late: the money is gone and the ceiling did nothing.

    Expressed as a property of the API — `guard()` raises, and it is the caller's
    entry point, so a call site that forgets it is visible in review rather than
    only in the bill.
    """
    cost = tracker(llm_daily_budget_inr=Decimal("1.00"))
    cost.record(Usage(input_tokens=10_000, output_tokens=1_000), SONNET)
    assert cost.is_open is True
    with pytest.raises(BudgetExhausted):
        cost.guard()


def test_the_warning_threshold_caps_generation_before_the_breaker_opens() -> None:
    cost = tracker(llm_daily_budget_inr=Decimal("10.00"), llm_budget_warn_pct=50)
    cost.record(Usage(input_tokens=4_000, output_tokens=600), SONNET)  # 1.85, 18%
    assert cost.should_cap_generation is False

    cost.record(Usage(input_tokens=10_000, output_tokens=1_000), SONNET)  # ~5.9 total
    assert cost.should_cap_generation is True
    assert cost.is_open is False, "warning must not stop the run"


def test_a_run_that_stopped_early_keeps_what_it_did() -> None:
    """Opening the breaker halts further work; it does not roll back."""
    cost = tracker(llm_daily_budget_inr=Decimal("1.00"))
    cost.record(Usage(input_tokens=10_000, output_tokens=1_000), SONNET)
    assert cost.is_open is True
    assert cost.calls == 1
    assert cost.spent_inr > 0


# --------------------------------------------------------------------------
# Totals and reporting
# --------------------------------------------------------------------------


def test_totals_accumulate_across_calls() -> None:
    cost = tracker()
    for _ in range(3):
        cost.record(Usage(input_tokens=1_000, output_tokens=100, cached_input_tokens=50), SONNET)
    assert cost.calls == 3
    assert cost.input_tokens == 3_000
    assert cost.output_tokens == 300
    assert cost.cached_input_tokens == 150


def test_a_failed_call_is_counted_but_not_priced() -> None:
    """A failure is not free, but its spend is unattributable rather than zero.

    The provider billed whatever it read before failing; the usage block rarely
    survives the error. Counting the failure separately says that honestly
    instead of implying the call cost nothing.
    """
    cost = tracker()
    cost.record_failure()
    assert cost.failures == 1
    assert cost.spent_inr == Decimal("0.00")
    assert cost.calls == 0


def test_the_stats_rollup_carries_cost_as_a_string() -> None:
    """A cost through binary floating point no longer adds up over a month."""
    cost = tracker()
    cost.record(Usage(input_tokens=4_000, output_tokens=600), SONNET)
    cost.record_repair()
    cost.record_cache_hit()
    stats = cost.as_stats()

    assert stats["llm_calls"] == 1
    assert stats["llm_repair_retries"] == 1
    assert stats["llm_cache_hits"] == 1
    assert stats["llm_budget_open"] is False
    assert isinstance(stats["llm_cost_inr"], str)
    assert Decimal(str(stats["llm_cost_inr"])) == Decimal("1.85")


def test_the_backfill_estimate_can_be_reproduced_from_the_model() -> None:
    """1,519 survivors at 4k in / 600 out should land near the quoted 2,807.

    The figure quoted to the operator came from this arithmetic. Pinning it
    means a change to the pricing model that would have moved that number shows
    up here rather than in a conversation months later.
    """
    per_call = cost_inr(Usage(input_tokens=4_000, output_tokens=600), SONNET, inr_per_usd=INR)
    total = per_call * 1_519
    assert Decimal("2_700") < total < Decimal("2_900")
