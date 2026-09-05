"""SourceHttpClient: retry, backoff, rate limiting, robots and the size cap."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from scout_careers.common.errors import (
    CircuitOpen,
    RateLimitTimeout,
    ResponseTooLarge,
    RobotsDenied,
    SchemaDriftError,
    TransportError,
    UpstreamHttpError,
)
from scout_careers.common.types import AtsType
from scout_careers.sources.http import (
    MAX_DELAY_S,
    RATE_LIMITS,
    InRunCircuitBreaker,
    RateLimitRule,
    RedisTokenBucket,
    RobotsPolicy,
    SourceHttpClient,
    backoff_delay,
    build_client,
    build_source_client,
    parse_retry_after,
)

ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"
GREENHOUSE = "https://boards-api.greenhouse.io"
UNKNOWN_HOST = "https://careers.acme-corp.example"


class RecordingLimiter:
    """Counts acquisitions so 'a retry re-acquires a token' is provable."""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.calls: list[tuple[AtsType, str, RateLimitRule]] = []
        self._fail_after = fail_after

    async def acquire(
        self,
        *,
        adapter: AtsType,
        bucket_key: str,
        rule: RateLimitRule,
        deadline_s: float,
    ) -> float:
        del deadline_s
        if self._fail_after is not None and len(self.calls) >= self._fail_after:
            raise RateLimitTimeout(f"no token for {adapter.value}:{bucket_key}")
        self.calls.append((adapter, bucket_key, rule))
        return 0.0


class RecordingSleep:
    """Replaces asyncio.sleep so backoff is observable and instant."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def make_client(
    settings: Any,
    http: httpx.AsyncClient,
    *,
    limiter: Any | None = None,
    sleep: Any | None = None,
    breaker: InRunCircuitBreaker | None = None,
) -> SourceHttpClient:
    return SourceHttpClient(
        http,
        settings=settings,
        source_id=7,
        adapter=AtsType.GREENHOUSE,
        bucket_key="boards-api.greenhouse.io",
        limiter=limiter or RecordingLimiter(),
        robots=RobotsPolicy(http, user_agent=settings.source_user_agent, cache_ttl_s=60),
        breaker=breaker or InRunCircuitBreaker(threshold=settings.circuit_breaker_failures),
        sleep=sleep or RecordingSleep(),
    )


def mock_robots(host: str = GREENHOUSE, body: str = ROBOTS_ALLOW_ALL) -> None:
    respx.get(f"{host}/robots.txt").mock(return_value=httpx.Response(200, text=body))


# --------------------------------------------------------------------------
# backoff_delay / Retry-After
# --------------------------------------------------------------------------


def test_backoff_is_bounded_by_the_ceiling_for_each_attempt() -> None:
    for attempt in range(6):
        ceiling = min(MAX_DELAY_S, 1.0 * 2**attempt)
        for _ in range(50):
            delay = backoff_delay(attempt, None)
            assert 0.0 <= delay <= ceiling


def test_retry_after_wins_and_is_capped() -> None:
    assert backoff_delay(0, 2.0) == 2.0
    assert backoff_delay(3, 5.0) == 5.0
    assert backoff_delay(0, 120.0) == MAX_DELAY_S


def test_parse_retry_after_handles_both_forms() -> None:
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("  0 ") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("nonsense") is None
    # An HTTP-date in the past clamps to zero rather than going negative.
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


# --------------------------------------------------------------------------
# retry behaviour
# --------------------------------------------------------------------------


@respx.mock
async def test_retries_a_500_then_succeeds(settings) -> None:
    mock_robots()
    route = respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(
        side_effect=[
            httpx.Response(500, text="boom"),
            httpx.Response(200, json={"jobs": [{"id": 1}]}),
        ]
    )
    limiter = RecordingLimiter()
    sleep = RecordingSleep()

    async with build_client(settings) as http:
        client = make_client(settings, http, limiter=limiter, sleep=sleep)
        payload = await client.get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")

    assert payload == {"jobs": [{"id": 1}]}
    assert route.call_count == 2
    assert client.counters.retries == 1
    assert client.counters.requests == 2
    assert len(sleep.delays) == 1
    # A retry re-acquires a token like any other request: backing off and then
    # bursting is how a well-behaved client becomes a badly-behaved one.
    assert len(limiter.calls) == 2


@respx.mock
async def test_gives_up_after_four_attempts(settings) -> None:
    mock_robots()
    route = respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(return_value=httpx.Response(503))
    async with build_client(settings) as http:
        client = make_client(settings, http)
        with pytest.raises(UpstreamHttpError) as excinfo:
            await client.get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")

    assert route.call_count == 4  # 1 initial + 3 retries
    assert excinfo.value.status_code == 503


@respx.mock
async def test_does_not_retry_a_404(settings) -> None:
    mock_robots()
    route = respx.get(f"{GREENHOUSE}/v1/boards/gone/jobs").mock(return_value=httpx.Response(404))
    limiter = RecordingLimiter()
    async with build_client(settings) as http:
        client = make_client(settings, http, limiter=limiter)
        with pytest.raises(UpstreamHttpError) as excinfo:
            await client.get_json(f"{GREENHOUSE}/v1/boards/gone/jobs")

    assert route.call_count == 1
    assert excinfo.value.status_code == 404
    assert excinfo.value.error_code == "adapter.board_not_found"
    assert client.counters.retries == 0
    assert len(limiter.calls) == 1


@respx.mock
async def test_does_not_retry_a_403(settings) -> None:
    mock_robots()
    route = respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(return_value=httpx.Response(403))
    async with build_client(settings) as http:
        with pytest.raises(UpstreamHttpError):
            await make_client(settings, http).get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")
    assert route.call_count == 1


@respx.mock
async def test_retry_after_is_honoured_when_within_the_cap(settings) -> None:
    mock_robots()
    respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "3"}),
            httpx.Response(200, json=[]),
        ]
    )
    sleep = RecordingSleep()
    async with build_client(settings) as http:
        client = make_client(settings, http, sleep=sleep)
        assert await client.get_json(f"{GREENHOUSE}/v1/boards/acme/jobs") == []

    assert sleep.delays == [3.0]


@respx.mock
async def test_retry_after_beyond_the_cap_abandons_the_attempt(settings) -> None:
    mock_robots()
    route = respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(429, headers={"retry-after": "600"})
    )
    sleep = RecordingSleep()
    async with build_client(settings) as http:
        client = make_client(settings, http, sleep=sleep)
        with pytest.raises(UpstreamHttpError) as excinfo:
            await client.get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")

    # Beyond the cap the attempt is abandoned rather than blocking the run.
    assert route.call_count == 1
    assert sleep.delays == []
    assert excinfo.value.status_code == 429


@respx.mock
async def test_transport_error_retries_then_raises(settings) -> None:
    mock_robots()
    route = respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(
        side_effect=httpx.ConnectError("refused")
    )
    async with build_client(settings) as http:
        client = make_client(settings, http)
        with pytest.raises(TransportError) as excinfo:
            await client.get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")

    assert route.call_count == 4
    assert excinfo.value.error_code == "adapter.transport"


@respx.mock
async def test_retry_budget_stops_retrying(settings) -> None:
    mock_robots()
    tight = settings.model_copy(update={"source_retry_budget_s": 1})
    route = respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(return_value=httpx.Response(500))

    class SlowClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            self.now += 5.0
            return self.now

    async with build_client(tight) as http:
        client = SourceHttpClient(
            http,
            settings=tight,
            source_id=7,
            adapter=AtsType.GREENHOUSE,
            bucket_key="boards-api.greenhouse.io",
            limiter=RecordingLimiter(),
            robots=RobotsPolicy(http, user_agent=tight.source_user_agent, cache_ttl_s=60),
            breaker=InRunCircuitBreaker(threshold=99),
            sleep=RecordingSleep(),
            monotonic=SlowClock(),
        )
        with pytest.raises(UpstreamHttpError):
            await client.get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")

    assert route.call_count == 1


# --------------------------------------------------------------------------
# rate limiting and the circuit breaker
# --------------------------------------------------------------------------


@respx.mock
async def test_rate_limit_timeout_propagates(settings) -> None:
    mock_robots()
    respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(return_value=httpx.Response(200, json=[]))
    async with build_client(settings) as http:
        client = make_client(settings, http, limiter=RecordingLimiter(fail_after=0))
        with pytest.raises(RateLimitTimeout) as excinfo:
            await client.get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")
    assert excinfo.value.error_code == "adapter.rate_limited"


@respx.mock
async def test_breaker_opens_after_consecutive_failures(settings) -> None:
    mock_robots()
    route = respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(return_value=httpx.Response(500))
    breaker = InRunCircuitBreaker(threshold=2)
    async with build_client(settings) as http:
        client = make_client(settings, http, breaker=breaker)
        # Two 5xx open the breaker; the third attempt is short-circuited with no
        # request at all, which is what stops 40 sources on a down tenant from
        # each burning the retry budget.
        with pytest.raises(CircuitOpen):
            await client.get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")

    assert breaker.is_open("boards-api.greenhouse.io")
    assert route.call_count == 2


def test_breaker_success_clears_the_failure_count() -> None:
    breaker = InRunCircuitBreaker(threshold=2)
    breaker.record_failure("bucket")
    breaker.record_success("bucket")
    breaker.record_failure("bucket")
    assert breaker.is_open("bucket") is False
    breaker.check("bucket")


def test_crawl_delay_lowers_the_rate_and_never_raises_it() -> None:
    rule = RateLimitRule(5.0, 10)
    assert rule.lowered_to(2.0) == RateLimitRule(0.5, 1)
    # A delay implying a faster rate than ours must not raise it.
    assert rule.lowered_to(0.05).rate_per_s == 5.0
    assert rule.lowered_to(0.0) is rule


# --------------------------------------------------------------------------
# response size cap
# --------------------------------------------------------------------------


@respx.mock
async def test_oversized_response_is_failed_not_truncated(settings) -> None:
    mock_robots()
    tiny = settings.model_copy(update={"max_response_bytes": 1024})
    body = b'{"jobs": "' + b"x" * 4096 + b'"}'
    respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, content=body)
    )
    async with build_client(tiny) as http:
        client = make_client(tiny, http)
        with pytest.raises(ResponseTooLarge):
            await client.get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")


@respx.mock
async def test_declared_content_length_over_the_cap_is_refused(settings) -> None:
    mock_robots()
    tiny = settings.model_copy(update={"max_response_bytes": 1024})
    respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, headers={"content-length": "999999"}, content=b"{}")
    )
    async with build_client(tiny) as http:
        with pytest.raises(ResponseTooLarge):
            await make_client(tiny, http).get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")


@respx.mock
async def test_a_body_inside_the_cap_is_returned_whole(settings) -> None:
    mock_robots()
    payload = {"jobs": [{"id": index} for index in range(50)]}
    respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with build_client(settings) as http:
        assert (
            await make_client(settings, http).get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")
            == payload
        )


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


@respx.mock
async def test_unreachable_robots_fails_open_for_a_known_json_api(settings) -> None:
    respx.get(f"{GREENHOUSE}/robots.txt").mock(side_effect=httpx.ConnectError("no route"))
    respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    async with build_client(settings) as http:
        assert await make_client(settings, http).get_json(f"{GREENHOUSE}/v1/boards/acme/jobs") == {
            "jobs": []
        }


@respx.mock
async def test_unreachable_robots_fails_closed_for_an_unknown_host(settings) -> None:
    respx.get(f"{UNKNOWN_HOST}/robots.txt").mock(side_effect=httpx.ConnectError("no route"))
    route = respx.get(f"{UNKNOWN_HOST}/jobs").mock(return_value=httpx.Response(200, json=[]))
    async with build_client(settings) as http:
        client = SourceHttpClient(
            http,
            settings=settings,
            source_id=9,
            adapter=AtsType.WORKDAY,
            bucket_key="careers.acme-corp.example",
            limiter=RecordingLimiter(),
            robots=RobotsPolicy(http, user_agent=settings.source_user_agent, cache_ttl_s=60),
            breaker=InRunCircuitBreaker(threshold=5),
            sleep=RecordingSleep(),
        )
        with pytest.raises(RobotsDenied) as excinfo:
            await client.get_json(f"{UNKNOWN_HOST}/jobs")

    assert excinfo.value.error_code == "adapter.robots_denied"
    assert not route.called


@respx.mock
async def test_a_5xx_robots_also_fails_closed_for_an_unknown_host(settings) -> None:
    respx.get(f"{UNKNOWN_HOST}/robots.txt").mock(return_value=httpx.Response(503))
    async with build_client(settings) as http:
        client = SourceHttpClient(
            http,
            settings=settings,
            source_id=9,
            adapter=AtsType.WORKDAY,
            bucket_key="careers.acme-corp.example",
            limiter=RecordingLimiter(),
            robots=RobotsPolicy(http, user_agent=settings.source_user_agent, cache_ttl_s=60),
            breaker=InRunCircuitBreaker(threshold=5),
            sleep=RecordingSleep(),
        )
        with pytest.raises(RobotsDenied):
            await client.get_json(f"{UNKNOWN_HOST}/jobs")


@respx.mock
async def test_a_404_robots_means_no_restrictions(settings) -> None:
    respx.get(f"{UNKNOWN_HOST}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{UNKNOWN_HOST}/jobs").mock(return_value=httpx.Response(200, json=[1]))
    async with build_client(settings) as http:
        client = SourceHttpClient(
            http,
            settings=settings,
            source_id=9,
            adapter=AtsType.WORKDAY,
            bucket_key="careers.acme-corp.example",
            limiter=RecordingLimiter(),
            robots=RobotsPolicy(http, user_agent=settings.source_user_agent, cache_ttl_s=60),
            breaker=InRunCircuitBreaker(threshold=5),
            sleep=RecordingSleep(),
        )
        assert await client.get_json(f"{UNKNOWN_HOST}/jobs") == [1]


@respx.mock
async def test_a_disallow_is_never_bypassed(settings) -> None:
    mock_robots(body="User-agent: *\nDisallow: /v1/boards/\n")
    route = respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with build_client(settings) as http:
        with pytest.raises(RobotsDenied):
            await make_client(settings, http).get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")
    assert not route.called


@respx.mock
async def test_robots_is_fetched_once_per_host_per_run(settings) -> None:
    robots = respx.get(f"{GREENHOUSE}/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL)
    )
    respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(return_value=httpx.Response(200, json=[]))
    async with build_client(settings) as http:
        client = make_client(settings, http)
        await client.get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")
        await client.get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")
    assert robots.call_count == 1


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------


@respx.mock
async def test_non_json_body_raises_schema_drift(settings) -> None:
    mock_robots()
    respx.get(f"{GREENHOUSE}/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, html="<html>SPA shell</html>")
    )
    async with build_client(settings) as http:
        with pytest.raises(SchemaDriftError) as excinfo:
            await make_client(settings, http).get_json(f"{GREENHOUSE}/v1/boards/acme/jobs")
    assert excinfo.value.error_code == "adapter.schema_drift"


@respx.mock
async def test_post_json_is_retryable_because_search_is_a_read(settings) -> None:
    mock_robots()
    route = respx.post(f"{GREENHOUSE}/search").mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json={"total": 3})]
    )
    async with build_client(settings) as http:
        client = make_client(settings, http)
        assert await client.post_json(f"{GREENHOUSE}/search", json={"q": ""}) == {"total": 3}
    assert route.call_count == 2


@respx.mock
async def test_get_text_returns_the_body(settings) -> None:
    mock_robots()
    respx.get(f"{GREENHOUSE}/x").mock(return_value=httpx.Response(200, text="hello"))
    async with build_client(settings) as http:
        assert await make_client(settings, http).get_text(f"{GREENHOUSE}/x") == "hello"


def test_client_sends_the_documented_headers_and_ignores_ambient_proxies(settings) -> None:
    client = build_client(settings)
    try:
        assert client.headers["user-agent"] == settings.source_user_agent
        assert client.headers["accept"] == "application/json"
        assert client.headers["accept-language"] == "en-US,en;q=0.9"
        assert client.trust_env is False
    finally:
        pass


# --------------------------------------------------------------------------
# RedisTokenBucket — the Lua needs Redis, but the key, TTL and deadline logic
# around it does not, and that is where the bugs are.
# --------------------------------------------------------------------------


class FakeScript:
    """Stands in for a registered Lua script: replays a canned decision list."""

    def __init__(self, decisions: list[tuple[int, int]]) -> None:
        self.decisions = decisions
        self.calls: list[list[Any]] = []

    async def __call__(self, *, keys: list[str], args: list[Any]) -> list[int]:
        self.calls.append([*keys, *args])
        return list(self.decisions.pop(0)) if self.decisions else [1, 0]


class FakeRedis:
    def __init__(self, script: FakeScript) -> None:
        self._script = script

    def register_script(self, _source: str) -> FakeScript:
        return self._script


def test_bucket_key_and_ttl() -> None:
    assert (
        RedisTokenBucket.key(AtsType.WORKDAY, "adobe.wd5.myworkdayjobs.com")
        == "rl:workday:adobe.wd5.myworkdayjobs.com"
    )
    # 2x the time to refill a full bucket: 3 tokens at 1/s -> 6s.
    assert RedisTokenBucket.ttl_ms(RateLimitRule(1.0, 3)) == 6_000
    # Never shorter than two seconds, however fast the bucket refills.
    assert RedisTokenBucket.ttl_ms(RateLimitRule(50.0, 1)) == 2_000


async def test_token_bucket_waits_then_acquires() -> None:
    script = FakeScript([(0, 250), (0, 250), (1, 0)])
    sleep = RecordingSleep()
    bucket = RedisTokenBucket(FakeRedis(script), sleep=sleep, monotonic=lambda: 0.0)  # type: ignore[arg-type]

    waited = await bucket.acquire(
        adapter=AtsType.GREENHOUSE,
        bucket_key="boards-api.greenhouse.io",
        rule=RateLimitRule(5.0, 10),
        deadline_s=20.0,
    )

    assert sleep.delays == [0.25, 0.25]
    assert waited == pytest.approx(500.0)
    assert script.calls[0][0] == "rl:greenhouse:boards-api.greenhouse.io"


async def test_token_bucket_raises_past_the_deadline() -> None:
    script = FakeScript([(0, 5_000)] * 10)
    clock = iter([0.0, 0.0, 30.0, 60.0, 90.0])
    bucket = RedisTokenBucket(
        FakeRedis(script),  # type: ignore[arg-type]
        sleep=RecordingSleep(),
        monotonic=lambda: next(clock),
    )
    with pytest.raises(RateLimitTimeout):
        await bucket.acquire(
            adapter=AtsType.ASHBY,
            bucket_key="api.ashbyhq.com",
            rule=RateLimitRule(4.0, 8),
            deadline_s=20.0,
        )


def test_build_source_client_defaults_the_static_bucket(settings) -> None:
    http = build_client(settings)
    robots = RobotsPolicy(http, user_agent=settings.source_user_agent, cache_ttl_s=60)
    breaker = InRunCircuitBreaker(threshold=5)
    client = build_source_client(
        http,
        settings=settings,
        source_id=1,
        adapter=AtsType.GREENHOUSE,
        bucket_key=None,
        limiter=RecordingLimiter(),
        robots=robots,
        breaker=breaker,
    )
    assert client.bucket_key == "boards-api.greenhouse.io"

    # Per-tenant adapters share a bucket per host, so the caller must say which.
    with pytest.raises(ValueError, match="bucket_key is required"):
        build_source_client(
            http,
            settings=settings,
            source_id=2,
            adapter=AtsType.WORKDAY,
            bucket_key=None,
            limiter=RecordingLimiter(),
            robots=robots,
            breaker=breaker,
        )


def test_every_adapter_has_a_rate_limit_rule() -> None:
    assert set(RATE_LIMITS) == set(AtsType)
    for rule in RATE_LIMITS.values():
        assert rule.rate_per_s > 0
        assert rule.burst >= 1


# --------------------------------------------------------------------------
# robots.txt is read once per host per run
#
# `RobotsPolicy` documents "a 320-source run reads each host's robots.txt once".
# A live run with a cold Redis cache read boards-api.greenhouse.io/robots.txt
# EIGHT times — `source_concurrency`, not one. Nineteen Greenhouse sources start
# together, all miss the empty memo, all await the fetch, and all issue it
# before any of them writes. Harmless in bytes, wrong in kind: robots.txt is the
# file we read in order to be polite.
# --------------------------------------------------------------------------


@respx.mock
async def test_concurrent_sources_on_one_host_fetch_robots_once(settings) -> None:
    route = respx.get(f"{GREENHOUSE}/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL)
    )

    async with httpx.AsyncClient() as http:
        policy = RobotsPolicy(http, user_agent=settings.source_user_agent, cache_ttl_s=60)
        # Concurrent, not sequential: sequential passes with or without the
        # lock, so it would prove nothing about the bug.
        await asyncio.gather(
            *(policy.check(f"{GREENHOUSE}/v1/boards/board-{n}/jobs") for n in range(8))
        )

    assert route.call_count == 1


@respx.mock
async def test_a_second_host_is_not_blocked_by_the_first(settings) -> None:
    """The lock is per origin. One slow host must not serialise the whole run."""
    greenhouse = respx.get(f"{GREENHOUSE}/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL)
    )
    other = respx.get("https://api.lever.co/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS_ALLOW_ALL)
    )

    async with httpx.AsyncClient() as http:
        policy = RobotsPolicy(http, user_agent=settings.source_user_agent, cache_ttl_s=60)
        await asyncio.gather(
            policy.check(f"{GREENHOUSE}/v1/boards/a/jobs"),
            policy.check("https://api.lever.co/v0/postings/b"),
            policy.check(f"{GREENHOUSE}/v1/boards/c/jobs"),
            policy.check("https://api.lever.co/v0/postings/d"),
        )

    assert greenhouse.call_count == 1
    assert other.call_count == 1


@respx.mock
async def test_a_denial_is_still_raised_for_every_waiter(settings) -> None:
    """One fetch, but all eight callers must still be refused.

    Deduplicating the fetch must not deduplicate the *decision* — a waiter that
    got no answer because someone else asked would be a source silently allowed
    past robots.
    """
    respx.get(f"{GREENHOUSE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /")
    )

    async with httpx.AsyncClient() as http:
        policy = RobotsPolicy(http, user_agent=settings.source_user_agent, cache_ttl_s=60)
        results = await asyncio.gather(
            *(policy.check(f"{GREENHOUSE}/v1/boards/board-{n}/jobs") for n in range(8)),
            return_exceptions=True,
        )

    assert len(results) == 8
    assert all(isinstance(r, RobotsDenied) for r in results)
