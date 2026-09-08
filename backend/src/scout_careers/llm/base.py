"""The only interface a service is allowed to depend on.

AWS Bedrock is the default provider and Azure OpenAI the alternate; neither is
permitted to leak into calling code. Services depend on :class:`LLMClient`, and
a provider swap is a configuration change (AI_ARCHITECTURE.md §3.1).

Two design decisions here are load-bearing rather than stylistic:

**There is no ``complete()`` returning free text.** Everything except the
interactive stream goes through :meth:`LLMClient.structured`, so schema
enforcement cannot be forgotten by omission — it is the only door.

**Aliases cross the interface, not model IDs.** A service asks for ``fast`` or
``strong`` and :mod:`~scout_careers.llm.router` resolves it. That is what lets an
eval pin a specific model without touching a service, and what keeps
``LLM_MODEL_FAST`` a deployment concern.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final, Literal, Protocol, TypeVar

from pydantic import BaseModel

#: Bound to the schema type a call validates against. The dataclass below uses
#: PEP 695 syntax directly; this TypeVar carries the same bound into the
#: Protocol's method signatures, where the parameter has to be nameable in both
#: the argument (``schema: type[T]``) and the return (``LLMResponse[T]``).
T = TypeVar("T", bound=BaseModel)

#: The two aliases. A third would mean a task that is neither routine nor
#: generative, and no such task exists in this pipeline (§4.1).
Alias = Literal["fast", "strong"]


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts as the provider reported them.

    Attributes:
        input_tokens: Billed input tokens.
        output_tokens: Billed output tokens.
        cached_input_tokens: Input tokens served from the provider's prompt
            cache, billed at a small fraction of the input rate. Reported
            separately because conflating them makes the cache look free when it
            is merely cheap, and makes a cache regression invisible.
        cache_write_tokens: Input tokens written *into* the cache. Billed above
            the normal input rate, so a cache that never gets read is a cost
            increase rather than a saving — which is exactly the failure this
            field exists to make visible.
    """

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMResponse[T: BaseModel]:
    """One validated model response.

    Attributes:
        value: The validated Pydantic instance. The point of the whole layer.
        raw_text: The payload as JSON. **In memory only** — never persisted and
            never logged, because it may contain job-description text
            (§11.2).
        model_id: The resolved provider model ID, not the alias. This is what
            ``requirement.model`` records, and what makes invariant 7 —
            nothing exists whose model cannot be named — true.
        prompt_version: ``family@version``.
        usage: Token counts, from the response rather than estimated.
        latency_ms: Wall clock for the call.
        attempts: 1 unless a repair retry fired.
        stop_reason: The provider's own termination reason. ``max_tokens`` here
            means a truncated answer that happened to validate.
    """

    value: T
    raw_text: str
    model_id: str
    prompt_version: str
    usage: Usage
    latency_ms: int
    attempts: int
    stop_reason: str


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base for every failure in this layer."""


class LLMTimeout(LLMError):
    """The call exceeded its deadline."""


class LLMRateLimited(LLMError):
    """The provider asked us to slow down. Retryable, with backoff."""


class LLMProviderUnavailable(LLMError):
    """Transport failure or a provider 5xx. Retryable."""


class LLMConfigError(LLMError):
    """The provider is not usable as configured. Not retryable."""


class SchemaViolation(LLMError):
    """The model returned something the schema refuses.

    A different failure from a transport error and counted separately: asking a
    rate limiter again later usually works, whereas asking a model the same
    question identically usually produces the same wrong shape.
    """

    def __init__(self, errors: list[dict[str, object]], raw: str) -> None:
        super().__init__(f"response failed schema validation ({len(errors)} error(s))")
        self.errors = errors
        #: Never logged. Held so the repair step can count, not quote.
        self.raw = raw


class SchemaEnforcementFailed(LLMError):
    """Every repair retry was used and the output still did not validate.

    Fatal for one item, never for the run. Each stage has a documented
    behaviour on this (§6.3), and every one of them fails closed.
    """

    def __init__(self, family: str, prompt_id: str, errors: list[dict[str, object]]) -> None:
        super().__init__(f"{family} ({prompt_id}) failed enforcement after repair")
        self.family = family
        self.prompt_id = prompt_id
        self.errors = errors


class BudgetExhausted(LLMError):
    """The daily budget breaker is open. No further model calls this run."""


# ---------------------------------------------------------------------------
# Call policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallPolicy:
    """Per-family limits.

    Attributes:
        timeout_s: Deadline for one attempt.
        max_transport_retries: Network, 429 and 5xx. Retrying helps.
        max_repair_retries: Schema violations. A *different* failure, counted
            separately — conflating the two produces the classic bug where a
            malformed-output loop burns the daily budget in ninety seconds.
        backoff_base_s: Exponential base.
        backoff_max_s: Ceiling on one sleep.
    """

    timeout_s: float
    max_transport_retries: int
    max_repair_retries: int
    backoff_base_s: float = 0.75
    backoff_max_s: float = 12.0

    def backoff(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Return the sleep before ``attempt``, with full jitter.

        Args:
            attempt: Zero-based retry number.
            rng: Injectable for deterministic tests.

        Returns:
            Seconds to sleep.

        Full jitter rather than a fixed ramp, because a run fires thirty
        extractions in a burst: without it every retry lands in the same
        millisecond and the second wave is throttled exactly like the first.
        """
        # 2.0, not 2: `int ** int` with a variable exponent is `Any` to a type
        # checker, because `2 ** -1` is a float. The Any then propagates through
        # every arithmetic operation downstream of it.
        ceiling = min(self.backoff_max_s, self.backoff_base_s * (2.0**attempt))
        return (rng or random).uniform(0, ceiling)

    def deadline_s(self) -> float:
        """Return the end-to-end bound for the whole call including retries.

        A stage must not be able to hang a run — the wall-clock budget is 15
        minutes and it is enforced (§3.4).
        """
        attempts = self.max_transport_retries + 1
        backoff_budget = sum(
            min(self.backoff_max_s, self.backoff_base_s * (2.0**n))
            for n in range(self.max_transport_retries)
        )
        return self.timeout_s * attempts + backoff_budget


#: Per family. A timeout is not retried at the same value — the second attempt
#: gets 1.5x, capped; a third does not happen (§3.4).
POLICY: Final[dict[str, CallPolicy]] = {
    "requirement_extraction": CallPolicy(
        timeout_s=25.0, max_transport_retries=3, max_repair_retries=1
    ),
    "coverage_judgement": CallPolicy(timeout_s=25.0, max_transport_retries=3, max_repair_retries=1),
    "mail_classification": CallPolicy(
        timeout_s=15.0, max_transport_retries=2, max_repair_retries=1
    ),
    "tailoring_plan": CallPolicy(timeout_s=60.0, max_transport_retries=2, max_repair_retries=2),
    "cover_letter": CallPolicy(timeout_s=60.0, max_transport_retries=2, max_repair_retries=1),
}

#: Which alias each family routes to (§4.2). Extraction and judgement are high
#: volume and tolerate a repair retry; composition is low volume and a worse
#: answer costs more than the token difference.
ROUTE: Final[dict[str, Alias]] = {
    "requirement_extraction": "fast",
    "coverage_judgement": "fast",
    "mail_classification": "fast",
    "tailoring_plan": "strong",
    "cover_letter": "strong",
}

#: Deterministic where the task has a right answer. Extraction and judgement are
#: not creative acts, and a temperature above zero there buys variance in the
#: one place the eval is trying to measure.
TEMPERATURE: Final[dict[str, float]] = {
    "requirement_extraction": 0.0,
    "coverage_judgement": 0.0,
    "mail_classification": 0.0,
    "tailoring_plan": 0.3,
    "cover_letter": 0.4,
}

#: Output ceilings. A bound on a runaway generation, not a target.
#:
#: A ceiling only does its job when it sits near the real distribution: 4,000 for
#: extraction is not a safety margin, it is the difference between a runaway
#: generation costing 3x and being cut off.
#:
#: Extraction is 2,600, not the 1,200 of AI_ARCHITECTURE.md §5.2. That figure came
#: from an assumed "typical ~700" written before any posting had been extracted.
#: Measured: a 16-requirement posting emits ~1,200 output tokens, and the first
#: real backfill hit the ceiling on four of five calls — one of them returning an
#: empty requirement list because the generation was cut off. 2,600 is roughly
#: double the observed median, which leaves room for the 40-requirement postings
#: the schema permits while still catching a genuine runaway. Bedrock's
#: ``max_tokens`` stop reason is now a schema failure (``llm/bedrock.py``), so a
#: truncation is refused rather than stored — but a ceiling the ordinary case
#: keeps hitting turns that refusal into a per-posting outage, which is why the
#: number had to move as well.
#: Output ceilings per family.
#:
#: ``requirement_extraction`` must be able to carry the *schema's own* maximum.
#: ``MAX_REQUIREMENTS`` is 40, and the 2026-09-07 backfill priced a requirement
#: at 60–70 output tokens (40 requirements → 2,401; 38 → 2,364; 36 → 2,556). At
#: 2,600 a forty-requirement answer only fits when every line is terse, so a
#: verbose job description truncates — and a truncated tool call is not a
#: partial answer, it is no answer: 46 of 100 postings failed that way in one
#: run, at roughly ₹150 spent on nothing.
#:
#: This was already raised once, 1,200 → 2,600, against a five-posting sample
#: that never reached the cap. The number now comes from the schema instead:
#: 40 x 80 tokens of headroom, rounded, so the ceiling can always express what
#: the contract permits. If ``MAX_REQUIREMENTS`` rises, this rises with it.
MAX_OUTPUT_TOKENS: Final[dict[str, int]] = {
    "requirement_extraction": 4_000,
    "coverage_judgement": 2_000,
    "mail_classification": 500,
    "tailoring_plan": 3_000,
    "cover_letter": 2_000,
}


def policy_for(family: str) -> CallPolicy:
    """Return the policy for a prompt family.

    Args:
        family: The prompt family name.

    Returns:
        Its policy.

    Raises:
        LLMConfigError: When the family is unknown. A typo'd family that
            silently got a default policy would be a call with the wrong
            timeout and the wrong retry budget, discovered in production.
    """
    try:
        return POLICY[family]
    except KeyError:
        raise LLMConfigError(
            f"unknown prompt family {family!r}; known: {', '.join(sorted(POLICY))}"
        ) from None


class LLMClient(Protocol):
    """The interface. Services depend on this and never on a provider module."""

    name: str

    async def structured(
        self,
        *,
        model: Alias,
        system: str,
        user: str,
        schema: type[T],
        temperature: float,
        max_output_tokens: int,
        timeout_s: float,
        cache_system: bool = False,
    ) -> LLMResponse[T]:
        """Make one request and return a schema-validated response.

        Args:
            model: ``fast`` or ``strong``. An alias, never a model ID.
            system: The trusted system prompt.
            user: The rendered user message, with any untrusted content already
                enveloped by :mod:`~scout_careers.llm.guard`.
            schema: The Pydantic model the response must satisfy.
            temperature: Sampling temperature.
            max_output_tokens: Ceiling on the response.
            timeout_s: Deadline for this attempt.
            cache_system: Ask the provider to cache the system block. Worth it
                when the same system prompt is about to be sent many times in a
                burst, which is the shape of the extraction stage.

        Returns:
            The validated response.

        Raises:
            SchemaViolation: The payload did not validate.
            LLMTimeout, LLMRateLimited, LLMProviderUnavailable: Transport.
        """
        ...

    def stream_text(
        self,
        *,
        model: Alias,
        system: str,
        user: str,
        temperature: float,
        max_output_tokens: int,
        timeout_s: float,
    ) -> AsyncIterator[str]:
        """Stream tokens. Operator-initiated interactive generation only.

        Not ``async def``: an async-generator function is annotated with a plain
        ``def`` returning ``AsyncIterator``, and callers write
        ``async for chunk in client.stream_text(...)``. Declaring it ``async``
        would mean "await it, then iterate what it returns" — a different and
        ambiguous contract.
        """
        ...

    async def healthcheck(self) -> bool:
        """Cheap reachability probe, for ``GET /api/v1/health``."""
        ...

    async def aclose(self) -> None:
        """Release provider resources."""
        ...


__all__ = [
    "MAX_OUTPUT_TOKENS",
    "POLICY",
    "ROUTE",
    "TEMPERATURE",
    "Alias",
    "BudgetExhausted",
    "CallPolicy",
    "LLMClient",
    "LLMConfigError",
    "LLMError",
    "LLMProviderUnavailable",
    "LLMRateLimited",
    "LLMResponse",
    "LLMTimeout",
    "SchemaEnforcementFailed",
    "SchemaViolation",
    "Usage",
    "policy_for",
]
