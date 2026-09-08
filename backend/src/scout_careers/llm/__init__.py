"""The LLM layer. Services depend on the Protocol in ``base``, never a provider.

Import contracts keep that honest: ``llm/`` may not import ``db/`` or
``ingest/``, so a provider module cannot quietly acquire a database session and
start persisting things the caller did not ask for.
"""

from __future__ import annotations

from scout_careers.llm.base import (
    Alias,
    BudgetExhausted,
    CallPolicy,
    LLMClient,
    LLMConfigError,
    LLMError,
    LLMProviderUnavailable,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
    SchemaEnforcementFailed,
    SchemaViolation,
    Usage,
)
from scout_careers.llm.cost import Prices, RunCost, cost_inr, prices_for
from scout_careers.llm.guard import envelope, looks_suspicious, sanitise
from scout_careers.llm.router import ModelRouter

__all__ = [
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
    "ModelRouter",
    "Prices",
    "RunCost",
    "SchemaEnforcementFailed",
    "SchemaViolation",
    "Usage",
    "cost_inr",
    "envelope",
    "looks_suspicious",
    "prices_for",
    "sanitise",
]
