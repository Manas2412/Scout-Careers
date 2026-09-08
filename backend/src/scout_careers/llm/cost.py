"""What a run actually spent, and the breaker that stops it spending more.

Cost is **computed from the ``usage`` block on every response**, never
estimated. That distinction is the whole point: an estimate tracks what we
believed about token counts when we wrote the estimate, and a cost regression
then shows up on the invoice rather than in the digest.

The breaker is not primarily about frugality. It is about a repair-retry loop
against a mis-authored prompt, which can spend a month's budget in ninety
seconds with nobody watching. A ceiling bounds that in code; an invoice does not.

Two thresholds (AI_ARCHITECTURE.md §3.4):

- **warn** — a warning is logged and the expensive stage is capped to its top
  items. The run still completes and still produces a digest.
- **open** — no further model calls are made. Remaining items are left for
  tomorrow and the digest says so. Items already done are kept: a run that
  stopped early is worth more than a run that rolled back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from scout_careers.common.config import Settings
from scout_careers.common.logging import get_logger
from scout_careers.llm.base import Alias, BudgetExhausted, Usage

log = get_logger(__name__)

_PER_MILLION: Final = Decimal(1_000_000)

#: What a cached input token costs relative to a fresh one. Providers bill cache
#: reads at roughly a tenth of the input rate; the exact figure varies, and 0.10
#: is the conservative published value for Anthropic models on Bedrock.
CACHE_READ_MULTIPLIER: Final = Decimal("0.10")

#: What writing to the cache costs relative to a fresh input token. Above 1.0 —
#: seeding the cache is more expensive than not using it, and only pays back if
#: the entry is then read. Modelling it as free would hide the case this is here
#: to expose: a cache that is written every call and never read.
CACHE_WRITE_MULTIPLIER: Final = Decimal("1.25")


@dataclass(frozen=True, slots=True)
class Prices:
    """USD per million tokens, per alias.

    Attributes:
        input_usd: Fresh input tokens.
        output_usd: Output tokens.
    """

    input_usd: Decimal
    output_usd: Decimal


def prices_for(settings: Settings, alias: Alias | str) -> Prices:
    """Return the price table for an alias.

    Args:
        settings: Configuration holding the four price values.
        alias: ``fast`` or ``strong``.

    Returns:
        Its prices.

    Raises:
        KeyError: On an unknown alias.
    """
    table = {
        "fast": Prices(settings.llm_price_fast_in, settings.llm_price_fast_out),
        "strong": Prices(settings.llm_price_strong_in, settings.llm_price_strong_out),
    }
    return table[alias]


def cost_inr(usage: Usage, prices: Prices, *, inr_per_usd: Decimal) -> Decimal:
    """Cost one call, in rupees.

    Args:
        usage: Token counts as the provider reported them.
        prices: USD per million tokens for the alias used.
        inr_per_usd: Conversion rate.

    Returns:
        Cost in INR, quantised to paise.

    Cached reads and cache writes are priced separately from fresh input.
    Folding them together would make prompt caching look like a pure saving,
    when a cache that is written on every call and never read is a **cost
    increase** — the one outcome worth being able to see.
    """
    fresh = Decimal(usage.input_tokens) * prices.input_usd
    cached = Decimal(usage.cached_input_tokens) * prices.input_usd * CACHE_READ_MULTIPLIER
    written = Decimal(usage.cache_write_tokens) * prices.input_usd * CACHE_WRITE_MULTIPLIER
    output = Decimal(usage.output_tokens) * prices.output_usd
    usd = (fresh + cached + written + output) / _PER_MILLION
    return (usd * inr_per_usd).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(slots=True)
class RunCost:
    """Running totals for one run, and the breaker.

    Not thread-safe and not intended to be: one run, one instance, mutated by
    the single task that owns the run's accounting.

    Attributes:
        budget_inr: The ceiling. At 100% the breaker opens.
        warn_pct: Where the warning fires and the expensive stage is capped.
        inr_per_usd: Conversion rate for reporting.
        calls: Model calls made.
        input_tokens: Fresh input tokens billed.
        cached_input_tokens: Input tokens served from the provider cache.
        cache_write_tokens: Input tokens written into the cache.
        output_tokens: Output tokens billed.
        spent_inr: Total, from ``usage``.
        failures: Calls that ended in an error.
        repair_retries: Repair attempts across all calls.
        cache_hits: Extractions served from the content-hash cache, which cost
            nothing at all — distinct from a provider cache read, which is
            cheap.
        prices: Per-alias price tables. Held here rather than looked up by the
            caller so that costing a call needs only the alias — the thing the
            caller already has — and no second reference to ``Settings``.
    """

    budget_inr: Decimal
    warn_pct: int
    inr_per_usd: Decimal
    prices: Mapping[str, Prices] = field(default_factory=dict)
    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    spent_inr: Decimal = field(default_factory=lambda: Decimal("0.00"))
    failures: int = 0
    repair_retries: int = 0
    cache_hits: int = 0
    _warned: bool = False
    _opened: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> RunCost:
        """Build a tracker from configuration."""
        return cls(
            budget_inr=settings.llm_daily_budget_inr,
            warn_pct=settings.llm_budget_warn_pct,
            inr_per_usd=settings.llm_inr_per_usd,
            prices={alias: prices_for(settings, alias) for alias in ("fast", "strong")},
        )

    # -- accounting ---------------------------------------------------------

    def record(self, usage: Usage, alias_or_prices: Alias | str | Prices) -> Decimal:
        """Add one call's usage to the totals.

        Args:
            usage: Reported token counts.
            alias_or_prices: The alias used (``fast`` / ``strong``), or an
                explicit price table. The alias form is what callers use; the
                explicit form keeps the arithmetic testable without building a
                whole ``Settings``.

        Returns:
            What this call cost, in INR.

        Raises:
            KeyError: On an alias with no configured prices.
        """
        prices = (
            alias_or_prices if isinstance(alias_or_prices, Prices) else self.prices[alias_or_prices]
        )
        amount = cost_inr(usage, prices, inr_per_usd=self.inr_per_usd)
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.cached_input_tokens += usage.cached_input_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        self.output_tokens += usage.output_tokens
        self.spent_inr += amount
        self._check_thresholds()
        return amount

    def record_failure(self) -> None:
        """Count a call that ended in an error.

        A failed call is not free — the provider billed the input tokens it read
        before failing — but the usage block usually does not survive the error,
        so the spend is unattributable rather than zero. Counting the failure
        separately keeps that honest instead of implying it cost nothing.
        """
        self.failures += 1

    def record_repair(self) -> None:
        """Count one repair retry."""
        self.repair_retries += 1

    def record_cache_hit(self) -> None:
        """Count an extraction served from the content-hash cache."""
        self.cache_hits += 1

    # -- the breaker --------------------------------------------------------

    @property
    def fraction_used(self) -> Decimal:
        """Spend as a fraction of the budget."""
        if self.budget_inr <= 0:
            return Decimal(0)
        return self.spent_inr / self.budget_inr

    @property
    def is_open(self) -> bool:
        """Whether the breaker has tripped."""
        return self._opened

    @property
    def should_cap_generation(self) -> bool:
        """Whether the expensive stage should be capped to its top items."""
        return self._warned or self._opened

    def _check_thresholds(self) -> None:
        used = self.fraction_used
        if not self._warned and used >= Decimal(self.warn_pct) / 100:
            self._warned = True
            log.warning(
                "llm_budget_warning",
                spent_inr=str(self.spent_inr),
                budget_inr=str(self.budget_inr),
                pct=int(used * 100),
                action="generation capped to top items",
            )
        if not self._opened and used >= 1:
            self._opened = True
            log.error(
                "llm_budget_exhausted",
                spent_inr=str(self.spent_inr),
                budget_inr=str(self.budget_inr),
                calls=self.calls,
                action="no further model calls this run",
            )

    def guard(self) -> None:
        """Refuse a call when the breaker is open.

        Raises:
            BudgetExhausted: When the budget is spent.

        Checked *before* a call rather than after, because after is too late —
        the money is gone and the ceiling did nothing.
        """
        if self._opened:
            raise BudgetExhausted(
                f"daily budget of INR {self.budget_inr} is spent "
                f"({self.calls} calls, INR {self.spent_inr}); "
                "remaining items are left for the next run"
            )

    # -- reporting ----------------------------------------------------------

    def as_stats(self) -> dict[str, object]:
        """Return the rollup for ``run_log.stats``.

        Returns:
            Plain JSON-safe values. ``Decimal`` is rendered as a string rather
            than a float, because a cost that has been through binary floating
            point is a cost that no longer adds up when summed over a month.
        """
        return {
            "llm_calls": self.calls,
            "llm_input_tokens": self.input_tokens,
            "llm_cached_input_tokens": self.cached_input_tokens,
            "llm_cache_write_tokens": self.cache_write_tokens,
            "llm_output_tokens": self.output_tokens,
            "llm_cost_inr": str(self.spent_inr),
            "llm_failures": self.failures,
            "llm_repair_retries": self.repair_retries,
            "llm_cache_hits": self.cache_hits,
            "llm_budget_inr": str(self.budget_inr),
            "llm_budget_open": self._opened,
        }


__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_MULTIPLIER",
    "Prices",
    "RunCost",
    "cost_inr",
    "prices_for",
]
