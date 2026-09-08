"""The loop that turns a model call into a validated object, or a clean failure.

Two retry budgets, counted separately, because they are different failures:

- **Transport** — a 429, a 5xx, a dropped connection. Asking again later
  usually works. Exponential backoff with full jitter.
- **Repair** — the answer did not validate. Asking again *identically* usually
  produces the same wrong shape, so the retry carries the validation errors and
  asks for a corrected object.

Conflating them produces the classic failure where a malformed-output loop
retries thirty times in ninety seconds and spends the day's budget. That is why
``max_repair_retries`` is 1 or 2 while ``max_transport_retries`` is 2 or 3.

What this loop never does:

- **Never coerce.** No stripping unknown fields, no defaulting a missing enum,
  no parsing a number out of a string. A model that answered ``"level":
  "mostly met"`` did not understand the task, and mapping that to ``partial``
  invents an answer nobody gave.
- **Never fall back to free-text parsing.** There is no regex path.
- **Never accept a partial result.** Twelve of seventeen judgements is a
  failure, not something to merge with the next attempt.
- **Never retry forever.**

When the budget is gone, :class:`SchemaEnforcementFailed` is raised. It is fatal
for one item and never for the run; each stage has a documented behaviour and
every one of them fails closed (AI_ARCHITECTURE.md §6.3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from scout_careers.common.logging import get_logger
from scout_careers.llm.base import (
    MAX_OUTPUT_TOKENS,
    ROUTE,
    TEMPERATURE,
    CallPolicy,
    LLMClient,
    LLMProviderUnavailable,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
    SchemaEnforcementFailed,
    SchemaViolation,
    T,
    policy_for,
)
from scout_careers.llm.cost import RunCost
from scout_careers.llm.registry import Prompt, render

log = get_logger(__name__)

#: A timeout is not retried at the same deadline — the next attempt gets this
#: much more, capped. A third attempt at a timeout does not happen (§3.4).
TIMEOUT_GROWTH = 1.5
TIMEOUT_CEILING_S = 90.0


def repair_message(original: str, errors: Sequence[Mapping[str, object]]) -> str:
    """Build the retry message.

    Args:
        original: The user message from the first attempt.
        errors: Pydantic validation errors, or notes from a post-validator.

    Returns:
        The original request, plus the exact validation failures, plus a
        restatement of what is wanted.

    It restates the request and appends the errors. It deliberately does **not**
    show the model its own malformed output to fix: that reliably produces a
    minimally-patched version of a wrong answer rather than a right one, because
    the model anchors on what it already wrote.
    """
    lines = []
    for error in errors:
        location = error.get("loc")
        path = ".".join(str(part) for part in location) if isinstance(location, tuple) else ""
        message = str(error.get("msg", "invalid"))
        lines.append(f"- {path}: {message}" if path else f"- {message}")
    problems = "\n".join(lines)
    return (
        f"{original}\n\n"
        "Your previous response was rejected by the schema validator with these "
        f"errors:\n{problems}\n\n"
        "Produce a corrected object that satisfies the schema. Do not explain "
        "the correction; call the tool with the corrected value."
    )


async def with_transport_retries(
    call: Callable[[float], object],
    *,
    policy: CallPolicy,
    family: str,
    sleep: Callable[[float], object] | None = None,
) -> object:
    """Run one attempt with bounded transport retries.

    Args:
        call: Takes a timeout and performs the call.
        policy: Supplies the budgets and the backoff.
        family: For the log line.
        sleep: Injectable, so a test does not wait.

    Returns:
        Whatever ``call`` returned.

    Raises:
        LLMTimeout, LLMRateLimited, LLMProviderUnavailable: When the budget is
            exhausted. The last failure is the one that propagates.
    """
    napper = sleep or asyncio.sleep
    timeout_s = policy.timeout_s
    last: Exception | None = None

    for attempt in range(policy.max_transport_retries + 1):
        try:
            return await call(timeout_s)  # type: ignore[misc]
        except (LLMRateLimited, LLMProviderUnavailable, LLMTimeout) as exc:
            last = exc
            if attempt == policy.max_transport_retries:
                break
            if isinstance(exc, LLMTimeout):
                # A second attempt at the same deadline against a model that is
                # simply slow today just buys the same timeout again.
                timeout_s = min(TIMEOUT_CEILING_S, timeout_s * TIMEOUT_GROWTH)
            delay = policy.backoff(attempt)
            log.warning(
                "llm_transport_retry",
                family=family,
                attempt=attempt + 1,
                error_type=type(exc).__name__,
                delay_s=round(delay, 2),
                next_timeout_s=round(timeout_s, 1),
            )
            await napper(delay)  # type: ignore[misc]

    assert last is not None  # noqa: S101 - the loop cannot exit without one
    raise last


async def call_structured(
    client: LLMClient,
    *,
    family: str,
    prompt: Prompt,
    schema: type[T],
    fields: Mapping[str, object],
    untrusted: Mapping[str, str] | None = None,
    max_untrusted_tokens: int,
    post_validate: Callable[[T], list[str]] | None = None,
    cost: RunCost | None = None,
    cache_system: bool = False,
    sleep: Callable[[float], object] | None = None,
) -> LLMResponse[T]:
    """Make a call, validate it, repair once or twice, or fail cleanly.

    Args:
        client: The provider.
        family: Prompt family; selects the policy, alias, temperature and
            output ceiling.
        prompt: The versioned prompt.
        schema: What the answer must satisfy.
        fields: Trusted values for the template.
        untrusted: Content to envelope.
        max_untrusted_tokens: Per-slot ceiling for untrusted content.
        post_validate: Constraints Pydantic cannot express — database lookups,
            ID allow-lists, cross-field rules against state. Returns a list of
            problems; empty means fine.
        cost: The run's accounting. When supplied, the breaker is checked
            *before* each call and usage is recorded after each one.
        cache_system: Ask the provider to cache the system block.
        sleep: Injectable, for tests.

    Returns:
        The validated response, with ``attempts`` and ``prompt_version`` set.

    Raises:
        SchemaEnforcementFailed: Every repair retry used, still invalid.
        BudgetExhausted: The daily breaker is open.
    """
    policy = policy_for(family)
    alias = ROUTE[family]
    user = render(
        prompt.user_template,
        fields,
        untrusted,
        max_untrusted_tokens=max_untrusted_tokens,
    )
    # `Sequence`, not `list`: list is invariant, so a `list[dict[str, object]]`
    # from a SchemaViolation cannot be assigned to a `list[Mapping[...]]`. The
    # two producers here genuinely differ — Pydantic hands back dicts, the
    # post-validator builds its own — and a covariant read-only type is the
    # honest way to hold both rather than casting one into the other.
    errors: Sequence[Mapping[str, object]] = []

    for attempt in range(policy.max_repair_retries + 1):
        if cost is not None:
            cost.guard()

        message = user if attempt == 0 else repair_message(user, errors)

        async def one_attempt(timeout_s: float, _message: str = message) -> LLMResponse[T]:
            return await client.structured(
                model=alias,
                system=prompt.system,
                user=_message,
                schema=schema,
                temperature=TEMPERATURE[family],
                max_output_tokens=MAX_OUTPUT_TOKENS[family],
                timeout_s=timeout_s,
                cache_system=cache_system,
            )

        try:
            response = await with_transport_retries(
                one_attempt, policy=policy, family=family, sleep=sleep
            )
        except SchemaViolation as exc:
            # The model answered, badly. Its input tokens were billed and the
            # usage block did not survive, so the spend is real but
            # unattributable — counted, not priced.
            errors = exc.errors
            if cost is not None:
                cost.record_failure()
                cost.record_repair()
            log.warning(
                "llm_repair_retry",
                family=family,
                prompt_version=prompt.id,
                attempt=attempt + 1,
                # Error paths and *types* only. Never the payload: it can carry
                # job-description text (§11.2).
                #
                # The type is what makes the line diagnostic rather than
                # decorative. Truncation raises with `type: max_tokens` and an
                # empty `loc`; a model answering in prose raises with an empty
                # `loc` too. Logging only the path renders both as
                # `error_paths=['']`, which is what 101 failures looked like on
                # the 2026-09-07 backfill — indistinguishable, and diagnosed
                # only by reading `bedrock.py` to find out what could produce an
                # empty location. A pydantic error `type` is a fixed enum, never
                # user content, so it costs nothing to say.
                error_count=len(errors),
                error_paths=[str(e.get("loc", "")) for e in errors][:10],
                error_types=[str(e.get("type", "unknown")) for e in errors][:10],
            )
            continue

        assert isinstance(response, LLMResponse)  # noqa: S101 - narrowing the callback's object
        typed: LLMResponse[T] = response

        if cost is not None:
            amount = cost.record(typed.usage, alias)
            log.info(
                "llm_call",
                family=family,
                prompt_version=prompt.id,
                prompt_sha256=prompt.sha256,
                model_id=typed.model_id,
                alias=alias,
                input_tokens=typed.usage.input_tokens,
                output_tokens=typed.usage.output_tokens,
                cached_input_tokens=typed.usage.cached_input_tokens,
                latency_ms=typed.latency_ms,
                attempts=attempt + 1,
                stop_reason=typed.stop_reason,
                cost_inr=str(amount),
            )

        # Constraints the schema cannot express. Checked after validation
        # because they need a typed object to inspect.
        if post_validate and (problems := post_validate(typed.value)):
            errors = [{"msg": problem} for problem in problems]
            if cost is not None:
                cost.record_repair()
            log.warning(
                "llm_post_validation_failed",
                family=family,
                prompt_version=prompt.id,
                attempt=attempt + 1,
                problem_count=len(problems),
            )
            continue

        return replace(typed, attempts=attempt + 1, prompt_version=prompt.id)

    log.error(
        "llm_enforcement_failed",
        family=family,
        prompt_version=prompt.id,
        attempts=policy.max_repair_retries + 1,
        error_count=len(errors),
        error_types=[str(e.get("type", "unknown")) for e in errors][:10],
    )
    raise SchemaEnforcementFailed(family, prompt.id, [dict(error) for error in errors])


__all__ = [
    "TIMEOUT_CEILING_S",
    "TIMEOUT_GROWTH",
    "call_structured",
    "repair_message",
    "with_transport_retries",
]
