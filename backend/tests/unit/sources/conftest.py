"""Shared harness for the adapter tests.

Everything here is offline. The repository-level ``conftest`` already patches
``socket`` to raise, so a request that escapes ``respx`` fails loudly instead of
quietly reaching an employer — which is the property that makes "no adapter test
touches the network" a fact rather than an intention.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import respx

from scout_careers.common.config import Settings
from scout_careers.common.types import AtsType
from scout_careers.sources.http import (
    InRunCircuitBreaker,
    NullRateLimiter,
    RobotsPolicy,
    SourceHttpClient,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "sources"

ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"

GREENHOUSE_HOST = "https://boards-api.greenhouse.io"
LEVER_HOST = "https://api.lever.co"
ASHBY_HOST = "https://api.ashbyhq.com"


def load_json(name: str) -> Any:
    """Load a captured JSON response fixture.

    Args:
        name: File name under ``tests/fixtures/sources``.

    Returns:
        The decoded document.
    """
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_html(name: str) -> str:
    """Load a captured alert-email fixture.

    Args:
        name: File name under ``tests/fixtures/sources/alerts``.

    Returns:
        The message HTML.
    """
    return (FIXTURES / "alerts" / name).read_text(encoding="utf-8")


async def instant_sleep(_delay: float) -> None:
    """Stand in for ``asyncio.sleep`` so backoff is instant in tests."""
    return None


def mock_robots(host: str, body: str = ROBOTS_ALLOW_ALL) -> None:
    """Register an allow-all robots.txt for a host."""
    respx.get(f"{host}/robots.txt").mock(return_value=httpx.Response(200, text=body))


def make_http(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    adapter: AtsType,
    bucket_key: str,
    source_id: int = 1,
) -> SourceHttpClient:
    """Build the per-source client an adapter is constructed with.

    Args:
        settings: Test settings.
        client: The shared httpx client.
        adapter: The adapter type, which selects the rate-limit rule.
        bucket_key: The rate-limiting domain.
        source_id: The source being simulated.

    Returns:
        A ``SourceHttpClient`` with a limiter that never throttles and a sleep
        that returns immediately, so retry backoff is exercised without a test
        run spending real seconds asleep.
    """
    return SourceHttpClient(
        client,
        settings=settings,
        source_id=source_id,
        adapter=adapter,
        bucket_key=bucket_key,
        limiter=NullRateLimiter(),
        robots=RobotsPolicy(client, user_agent=settings.source_user_agent, cache_ttl_s=60),
        breaker=InRunCircuitBreaker(threshold=settings.circuit_breaker_failures),
        sleep=instant_sleep,
    )
