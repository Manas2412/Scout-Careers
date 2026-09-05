"""The shared HTTP client every adapter fetches through.

Constructed once per run by ``ingest/runner.py`` and injected. No adapter
constructs an ``httpx.AsyncClient`` of its own — that is a review failure.

Every request goes through the same six gates, in this order, and the order is
the point:

1. :func:`~scout_careers.sources.policy.assert_fetch_allowed` on the URL, and
   again on every redirect hop *before* it is followed, so a source that 302s to
   LinkedIn is refused before the denied host is contacted at all.
2. robots.txt, cached per host per run and per day in Redis, evaluated against
   our real user agent and against ``*``. A ``Crawl-delay`` lowers this host's
   bucket rate for the run; it never raises it.
3. The in-run circuit breaker for this ``bucket_key``.
4. A token-bucket lease, with a deadline. Past it the request raises
   ``RateLimitTimeout`` and the runner moves on.
5. The request, with bounded retry and full jitter.
6. A hard response-size cap. Over it the response is failed, never truncated.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import random
import time
import urllib.robotparser
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx
from redis.asyncio import Redis

from scout_careers.common.clock import utcnow
from scout_careers.common.config import Settings
from scout_careers.common.errors import (
    CircuitOpen,
    RateLimitTimeout,
    ResponseTooLarge,
    RobotsDenied,
    SchemaDriftError,
    TransportError,
    UpstreamHttpError,
)
from scout_careers.common.logging import get_logger
from scout_careers.common.types import AtsType
from scout_careers.sources.policy import assert_fetch_allowed

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# §4.1 The client
# ---------------------------------------------------------------------------

TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)

MAX_REDIRECTS = 5


def build_client(settings: Settings) -> httpx.AsyncClient:
    """Build the one client shared by every adapter in a run.

    One ``AsyncClient`` for the whole run, so connection pooling and HTTP/2
    multiplexing actually happen. ``trust_env=False`` stops an ambient
    ``HTTP_PROXY`` in the container from silently rerouting traffic.

    Args:
        settings: Supplies the user agent.

    Returns:
        A configured ``httpx.AsyncClient``. The caller owns closing it.
    """
    return httpx.AsyncClient(
        timeout=TIMEOUT,
        limits=LIMITS,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        http2=True,
        headers={
            "User-Agent": settings.source_user_agent,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        },
        trust_env=False,
    )


# ---------------------------------------------------------------------------
# §4.2 Retry, backoff and jitter
# ---------------------------------------------------------------------------

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
RETRYABLE_EXC = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)

MAX_ATTEMPTS = 4  # 1 initial + 3 retries
BASE_DELAY_S = 1.0
MAX_DELAY_S = 30.0

#: Methods whose effect is idempotent. Workday and Workable use POST for
#: *search*; those are reads with a POST body and are retryable. Nothing in this
#: system performs a state-changing upstream request, but the allow-list is
#: written out rather than assumed so it survives someone later adding one.
RETRYABLE_METHODS = frozenset({"GET", "HEAD", "POST"})


def backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with full jitter.

    Full jitter, not equal jitter: with 320 sources on one daily trigger,
    decorrelating retries matters more than tightening variance.

    Args:
        attempt: Zero-based attempt index that just failed.
        retry_after: Seconds from a ``Retry-After`` header, if the upstream sent
            one. It always wins over computed backoff.

    Returns:
        Seconds to wait before the next attempt.
    """
    if retry_after is not None:
        return min(retry_after, MAX_DELAY_S)
    ceiling = min(MAX_DELAY_S, BASE_DELAY_S * 2**attempt)
    return random.uniform(0.0, ceiling)  # noqa: S311 - jitter, not a secret


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header, in either permitted form.

    Args:
        value: Header value: delta-seconds, or an HTTP-date.

    Returns:
        Seconds to wait, or ``None`` when absent or unparseable. Negative
        values clamp to zero.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - utcnow()).total_seconds())


# ---------------------------------------------------------------------------
# §4.3 Rate limiting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """A token bucket's shape.

    Attributes:
        rate_per_s: Sustained refill rate.
        burst: Bucket capacity.
    """

    rate_per_s: float
    burst: int

    def lowered_to(self, crawl_delay_s: float) -> RateLimitRule:
        """Return this rule reduced to honour a ``Crawl-delay`` directive.

        Args:
            crawl_delay_s: Seconds between requests the host asked for.

        Returns:
            A rule no faster than this one, never faster than the directive.
        """
        if crawl_delay_s <= 0:
            return self
        return RateLimitRule(rate_per_s=min(self.rate_per_s, 1.0 / crawl_delay_s), burst=1)


#: Conservative floors, chosen without reference to any published quota because
#: most of these APIs publish none. Where a vendor documents a limit, the bucket
#: is set to the lower of the two. The system never probes for a limit by
#: exceeding it.
RATE_LIMITS: dict[AtsType, RateLimitRule] = {
    AtsType.GREENHOUSE: RateLimitRule(5.0, 10),
    AtsType.LEVER: RateLimitRule(5.0, 10),
    AtsType.ASHBY: RateLimitRule(4.0, 8),
    AtsType.WORKDAY: RateLimitRule(1.0, 3),
    AtsType.SMARTRECRUITERS: RateLimitRule(3.0, 6),
    AtsType.WORKABLE: RateLimitRule(3.0, 6),
    AtsType.RECRUITEE: RateLimitRule(2.0, 4),
    AtsType.GOOGLE: RateLimitRule(1.0, 2),
    AtsType.AMAZON: RateLimitRule(1.0, 2),
    AtsType.MICROSOFT: RateLimitRule(1.0, 2),
    # Not HTTP to an employer at all — Gmail's own quota units govern.
    AtsType.MAIL_ALERT: RateLimitRule(5.0, 10),
    # No adapter class; postings arrive through the import endpoint.
    AtsType.MANUAL: RateLimitRule(1.0, 1),
}

#: Fixed rate-limiting domains. Adapters whose bucket is per tenant supply their
#: own key ({host} for workday, {company}.recruitee.com for recruitee).
STATIC_BUCKET_KEYS: dict[AtsType, str] = {
    AtsType.GREENHOUSE: "boards-api.greenhouse.io",
    AtsType.LEVER: "api.lever.co",
    AtsType.ASHBY: "api.ashbyhq.com",
    AtsType.SMARTRECRUITERS: "api.smartrecruiters.com",
    AtsType.WORKABLE: "apply.workable.com",
    AtsType.GOOGLE: "careers.google.com",
    AtsType.AMAZON: "www.amazon.jobs",
    AtsType.MICROSOFT: "gcsservices.careers.microsoft.com",
    AtsType.MAIL_ALERT: "gmail",
}


class RateLimiter(Protocol):
    """Acquires one request lease from a shared bucket."""

    async def acquire(
        self,
        *,
        adapter: AtsType,
        bucket_key: str,
        rule: RateLimitRule,
        deadline_s: float,
    ) -> float:
        """Block until a token is available.

        Returns:
            Milliseconds spent waiting.

        Raises:
            RateLimitTimeout: When no token arrived within ``deadline_s``.
        """


#: Atomic check-and-decrement. Kept as a Lua script so that the read, the
#: refill computation and the decrement cannot interleave between two processes
#: — a scheduled run and a manual POST /runs/discovery share these buckets.
BUCKET_LUA_SCRIPT = """
local key   = KEYS[1]
local rate  = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now   = tonumber(ARGV[3])
local ttl   = tonumber(ARGV[4])

local state  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])

if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end

local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(burst, tokens + elapsed * rate)

local allowed = 0
local wait_ms = 0
if tokens >= 1.0 then
  tokens = tokens - 1.0
  allowed = 1
else
  wait_ms = math.ceil(((1.0 - tokens) / rate) * 1000.0)
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, ttl)
return {allowed, wait_ms}
"""


class RedisTokenBucket:
    """Per-bucket token buckets in Redis.

    Redis rather than process memory so limits survive a restart mid-run and
    are shared when a manual run overlaps the scheduled one.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self._sleep = sleep
        self._monotonic = monotonic
        self._script = redis.register_script(BUCKET_LUA_SCRIPT)

    @staticmethod
    def key(adapter: AtsType, bucket_key: str) -> str:
        """Return the Redis key for a bucket."""
        return f"rl:{adapter.value}:{bucket_key}"

    @staticmethod
    def ttl_ms(rule: RateLimitRule) -> int:
        """Return 2x the time to refill a full bucket, in milliseconds."""
        refill_s = rule.burst / rule.rate_per_s if rule.rate_per_s > 0 else 1.0
        return max(2_000, int(refill_s * 2 * 1000))

    async def acquire(
        self,
        *,
        adapter: AtsType,
        bucket_key: str,
        rule: RateLimitRule,
        deadline_s: float,
    ) -> float:
        """Acquire one token, blocking up to ``deadline_s``.

        Args:
            adapter: Adapter type, part of the key.
            bucket_key: The rate-limiting domain, not the source ID.
            rule: Rate and burst for this bucket.
            deadline_s: Total seconds we are willing to wait.

        Returns:
            Milliseconds waited.

        Raises:
            RateLimitTimeout: When the deadline passed without a token.
        """
        key = self.key(adapter, bucket_key)
        ttl = self.ttl_ms(rule)
        started = self._monotonic()
        waited_ms = 0.0

        while True:
            now_ms = int(self._monotonic() * 1000)
            raw: Any = await self._script(
                keys=[key],
                args=[rule.rate_per_s, rule.burst, now_ms, ttl],
            )
            allowed = int(raw[0])
            wait_ms = float(raw[1])
            if allowed:
                return waited_ms

            elapsed = self._monotonic() - started
            remaining = deadline_s - elapsed
            if remaining <= 0:
                raise RateLimitTimeout(
                    f"no token for {adapter.value}:{bucket_key} within {deadline_s:.0f}s"
                )
            nap = min(wait_ms / 1000.0, remaining)
            waited_ms += nap * 1000.0
            await self._sleep(nap)


class NullRateLimiter:
    """A limiter that never throttles. For unit tests and the manual importer."""

    def __init__(self) -> None:
        self.acquisitions: list[tuple[AtsType, str]] = []

    async def acquire(
        self,
        *,
        adapter: AtsType,
        bucket_key: str,
        rule: RateLimitRule,
        deadline_s: float,
    ) -> float:
        """Record the acquisition and return immediately."""
        del rule, deadline_s
        self.acquisitions.append((adapter, bucket_key))
        return 0.0


# ---------------------------------------------------------------------------
# §4.7 robots.txt
# ---------------------------------------------------------------------------

#: Documented public JSON APIs whose contract is the API itself. An unreachable
#: robots.txt does not block a run against these. For every other host an
#: unreachable robots.txt fails the source closed: a documented public API is an
#: invitation whose absence of a robots file is not a refusal, while an employer
#: careers host that will not serve robots.txt has told us nothing, and the
#: conservative reading wins.
ROBOTS_FAIL_OPEN_HOSTS: frozenset[str] = frozenset(
    {
        "boards-api.greenhouse.io",
        "api.lever.co",
        "api.ashbyhq.com",
        "api.smartrecruiters.com",
        "apply.workable.com",
        "gcsservices.careers.microsoft.com",
    }
)


@dataclass(slots=True)
class _RobotsEntry:
    parser: urllib.robotparser.RobotFileParser | None
    crawl_delay_s: float | None


def _parse_robots(body: str) -> urllib.robotparser.RobotFileParser:
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.splitlines())
    return parser


def _allow_all() -> urllib.robotparser.RobotFileParser:
    parser = urllib.robotparser.RobotFileParser()
    parser.parse([])
    return parser


class RobotsPolicy:
    """Fetches, caches and evaluates robots.txt.

    Cached in process for the run and in Redis for ``ROBOTS_CACHE_TTL_S``, so a
    320-source run reads each host's robots.txt once.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        cache_ttl_s: int,
        timeout_s: float = 10.0,
        redis: Redis | None = None,
        fail_open_hosts: frozenset[str] = ROBOTS_FAIL_OPEN_HOSTS,
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._cache_ttl_s = cache_ttl_s
        self._timeout_s = timeout_s
        self._redis = redis
        self._fail_open_hosts = fail_open_hosts
        self._memo: dict[str, _RobotsEntry] = {}
        #: One lock per origin, so concurrent sources on one host produce one
        #: fetch rather than ``source_concurrency`` of them. See :meth:`check`.
        self._loading: dict[str, asyncio.Lock] = {}

    @staticmethod
    def cache_key(origin: str) -> str:
        """Return the Redis key for a host's cached robots.txt."""
        return f"robots:{origin}"

    async def check(self, url: str) -> float | None:
        """Assert that ``url`` may be fetched, and report any crawl delay.

        Args:
            url: The absolute URL about to be requested.

        Returns:
            The host's ``Crawl-delay`` in seconds, or ``None``.

        Raises:
            RobotsDenied: When robots.txt disallows the path, or could not be
                read and this host is not on the fail-open allow-list.
        """
        parsed = httpx.URL(url)
        host = (parsed.host or "").lower()
        origin = f"{parsed.scheme}://{host}"

        entry = self._memo.get(origin)
        if entry is None:
            entry = await self._load_once(origin)

        if entry.parser is None:
            if host in self._fail_open_hosts:
                return None
            raise RobotsDenied(f"robots.txt unreachable for {host}; failing closed")

        target = str(parsed)
        if not entry.parser.can_fetch(self._user_agent, target) or not entry.parser.can_fetch(
            "*", target
        ):
            raise RobotsDenied(f"robots.txt disallows {host}{parsed.path}")

        return entry.crawl_delay_s

    async def _load_once(self, origin: str) -> _RobotsEntry:
        """Populate the memo for ``origin``, fetching at most once per run.

        Args:
            origin: ``scheme://host``.

        Returns:
            The cached entry.

        The plain check-then-act this replaces was a race, and a live run showed
        it: **eight** requests to ``boards-api.greenhouse.io/robots.txt`` in one
        run, which is ``source_concurrency``, not the one this class documents.
        Nineteen Greenhouse sources start together, all miss the empty memo, all
        await ``_load``, and all fetch before any of them writes.

        It only surfaces when the Redis cache is cold, so most runs hide it —
        and it is worth fixing anyway, because robots.txt is the file we read to
        be polite. Fetching it eight times is the opposite of the point.

        The re-check inside the lock is the half that matters: seven waiters
        wake up after the first has written, find the memo populated, and return
        without a request.
        """
        lock = self._loading.setdefault(origin, asyncio.Lock())
        async with lock:
            entry = self._memo.get(origin)
            if entry is None:
                entry = await self._load(origin)
                self._memo[origin] = entry
            return entry

    async def _load(self, origin: str) -> _RobotsEntry:
        cached = await self._redis_get(origin)
        if cached is not None:
            return self._entry_from_payload(cached)

        payload = await self._fetch(origin)
        await self._redis_set(origin, payload)
        return self._entry_from_payload(payload)

    def _entry_from_payload(self, payload: dict[str, Any]) -> _RobotsEntry:
        if not payload.get("reachable", False):
            return _RobotsEntry(parser=None, crawl_delay_s=None)
        body = str(payload.get("body", ""))
        parser = _parse_robots(body) if body else _allow_all()
        return _RobotsEntry(parser=parser, crawl_delay_s=self._crawl_delay(parser))

    def _crawl_delay(self, parser: urllib.robotparser.RobotFileParser) -> float | None:
        for agent in (self._user_agent, "*"):
            raw = parser.crawl_delay(agent)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    async def _fetch(self, origin: str) -> dict[str, Any]:
        url = f"{origin}/robots.txt"
        assert_fetch_allowed(url)
        try:
            response = await self._client.get(url, timeout=self._timeout_s, follow_redirects=True)
        except httpx.HTTPError:
            return {"reachable": False, "body": ""}

        if response.status_code == httpx.codes.OK:
            return {"reachable": True, "body": response.text}
        if 400 <= response.status_code < 500:
            # A served 4xx is an answer: no restrictions are published.
            return {"reachable": True, "body": ""}
        return {"reachable": False, "body": ""}

    async def _redis_get(self, origin: str) -> dict[str, Any] | None:
        if self._redis is None:
            return None
        raw = await self._redis.get(self.cache_key(origin))
        if raw is None:
            return None
        try:
            decoded: Any = jsonlib.loads(raw)
        except (ValueError, TypeError):
            return None
        return decoded if isinstance(decoded, dict) else None

    async def _redis_set(self, origin: str, payload: dict[str, Any]) -> None:
        if self._redis is None:
            return
        await self._redis.set(self.cache_key(origin), jsonlib.dumps(payload), ex=self._cache_ttl_s)


# ---------------------------------------------------------------------------
# §4.8 In-run circuit breaker
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InRunCircuitBreaker:
    """Per-``bucket_key`` breaker, in memory, for the length of one run.

    Stops 40 Workday sources on a down tenant from each burning 90 seconds of
    retry budget. The durable, cross-run breaker is
    ``source.consecutive_failures`` in Postgres and lives in ``ingest/``.
    """

    threshold: int
    _failures: dict[str, int] = field(default_factory=dict)
    _open: set[str] = field(default_factory=set)

    def check(self, bucket_key: str) -> None:
        """Short-circuit if the breaker is open for this bucket.

        Raises:
            CircuitOpen: When the bucket has already failed ``threshold`` times.
        """
        if bucket_key in self._open:
            raise CircuitOpen(f"circuit open for {bucket_key}")

    def record_failure(self, bucket_key: str) -> None:
        """Count a transport-or-5xx failure, opening the breaker at the threshold."""
        count = self._failures.get(bucket_key, 0) + 1
        self._failures[bucket_key] = count
        if count >= self.threshold:
            self._open.add(bucket_key)

    def record_success(self, bucket_key: str) -> None:
        """Reset the consecutive-failure count for this bucket."""
        self._failures.pop(bucket_key, None)

    def is_open(self, bucket_key: str) -> bool:
        """Report whether the breaker is open for this bucket."""
        return bucket_key in self._open


# ---------------------------------------------------------------------------
# The per-source facade
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceCounters:
    """What the source result reports about network behaviour."""

    requests: int = 0
    retries: int = 0
    rate_limit_wait_ms: int = 0


class SourceHttpClient:
    """Per-source facade over the shared httpx client.

    Adds: never-scrape enforcement, robots enforcement, rate-limit lease
    acquisition, circuit-breaker checks, retry/backoff, response-size capping
    and structured logging. Adapters call ``get_json`` / ``post_json`` /
    ``get_text`` and nothing else.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        settings: Settings,
        source_id: int,
        adapter: AtsType,
        bucket_key: str,
        limiter: RateLimiter,
        robots: RobotsPolicy,
        breaker: InRunCircuitBreaker,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._settings = settings
        self.source_id = source_id
        self.adapter = adapter
        self.bucket_key = bucket_key
        self._limiter = limiter
        self._robots = robots
        self._breaker = breaker
        self._sleep = sleep
        self._monotonic = monotonic
        self._rule = RATE_LIMITS[adapter]
        self.counters = SourceCounters()

    # -- public API --------------------------------------------------------

    @property
    def settings(self) -> Settings:
        """The run's configuration, read-only.

        Adapters need exactly one value from it — ``max_description_chars``, the
        §9.1 truncation bound — and reading it from the injected client is what
        keeps them from calling ``get_settings()`` themselves and acquiring a
        second, untestable configuration source.
        """
        return self._settings

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        url_template: str | None = None,
    ) -> Any:
        """GET a URL and parse the body as JSON.

        Args:
            url: Absolute URL.
            params: Query parameters.
            headers: Extra request headers.
            url_template: The pattern to log instead of the interpolated URL, so
                board tokens do not leak into logs.

        Returns:
            The decoded JSON document.

        Raises:
            SchemaDriftError: When the body is not valid JSON.
        """
        body = await self._request(
            "GET", url, params=params, headers=headers, url_template=url_template
        )
        return self._decode_json(body, url_template or _default_template(url))

    async def post_json(
        self,
        url: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        url_template: str | None = None,
    ) -> Any:
        """POST a JSON body and parse the JSON response.

        Workday and Workable use POST for *search*: these are reads with a POST
        body, and they retry like any other read.

        Args:
            url: Absolute URL.
            json: The request body.
            headers: Extra request headers.
            url_template: The pattern to log instead of the interpolated URL.

        Returns:
            The decoded JSON document.

        Raises:
            SchemaDriftError: When the body is not valid JSON.
        """
        body = await self._request(
            "POST", url, json_body=json, headers=headers, url_template=url_template
        )
        return self._decode_json(body, url_template or _default_template(url))

    async def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        url_template: str | None = None,
    ) -> str:
        """GET a URL and return the body decoded as UTF-8.

        Args:
            url: Absolute URL.
            params: Query parameters.
            headers: Extra request headers.
            url_template: The pattern to log instead of the interpolated URL.

        Returns:
            The response body as text.
        """
        body = await self._request(
            "GET", url, params=params, headers=headers, url_template=url_template
        )
        return body.decode("utf-8", errors="replace")

    # -- internals ---------------------------------------------------------

    def _decode_json(self, body: bytes, template: str) -> Any:
        try:
            return jsonlib.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            # Some Workday tenants answer 200 with the SPA HTML shell when the
            # Accept header is missing; that arrives here, not as a transport
            # error.
            raise SchemaDriftError(f"response from {template} was not JSON") from exc

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        url_template: str | None = None,
    ) -> bytes:
        template = url_template or _default_template(url)
        if method.upper() not in RETRYABLE_METHODS:
            raise TransportError(f"{method} is not on the retryable-read allow-list")

        started = self._monotonic()
        budget_s = float(self._settings.source_retry_budget_s)
        last_error: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                status, body, response_headers = await self._attempt(
                    method, url, params=params, headers=headers, json_body=json_body
                )
            except RETRYABLE_EXC as exc:
                self._breaker.record_failure(self.bucket_key)
                last_error = TransportError(f"{type(exc).__name__} from {template}")
                delay = self._retry_delay(attempt, started, budget_s, None)
                if delay is None:
                    raise last_error from exc
                await self._backoff(delay)
                continue

            if status in RETRYABLE_STATUS:
                if status >= httpx.codes.INTERNAL_SERVER_ERROR:
                    self._breaker.record_failure(self.bucket_key)
                retry_after = parse_retry_after(response_headers.get("retry-after"))
                last_error = UpstreamHttpError(f"HTTP {status} from {template}", status_code=status)
                delay = self._retry_delay(attempt, started, budget_s, retry_after)
                if delay is None:
                    self._log(template, status, started, None)
                    raise last_error
                await self._backoff(delay)
                continue

            if status >= httpx.codes.BAD_REQUEST:
                # A 404 on a board token means the board moved; retrying it three
                # times is 3x the noise and 0x the information.
                self._log(template, status, started, None)
                raise UpstreamHttpError(f"HTTP {status} from {template}", status_code=status)

            self._breaker.record_success(self.bucket_key)
            self._log(template, status, started, None)
            return body

        raise last_error or TransportError(f"no attempt succeeded for {template}")

    def _retry_delay(
        self,
        attempt: int,
        started: float,
        budget_s: float,
        retry_after: float | None,
    ) -> float | None:
        """Return the delay before the next attempt, or None to give up.

        Args:
            attempt: Zero-based index of the attempt that just failed.
            started: Monotonic timestamp the request began at.
            budget_s: ``SOURCE_RETRY_BUDGET_S``.
            retry_after: Seconds the upstream asked for, if any.

        Returns:
            Seconds to sleep, or ``None`` when this request is finished failing:
            attempts exhausted, a ``Retry-After`` past the cap, or a delay that
            would overrun the retry budget.
        """
        if attempt >= MAX_ATTEMPTS - 1:
            return None
        if retry_after is not None and retry_after > MAX_DELAY_S:
            # Beyond the cap the attempt is abandoned and the source is failed
            # for this run rather than blocking the run budget.
            return None
        delay = backoff_delay(attempt, retry_after)
        if (self._monotonic() - started) + delay > budget_s:
            return None
        return delay

    async def _backoff(self, delay: float) -> None:
        self.counters.retries += 1
        await self._sleep(delay)

    async def _attempt(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        json_body: Mapping[str, Any] | None,
    ) -> tuple[int, bytes, httpx.Headers]:
        """Issue one attempt, following redirects by hand so each hop is checked."""
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            # (1) never-scrape, before the host is contacted at all.
            assert_fetch_allowed(current)
            # (2) robots, which may lower this host's bucket rate for the run.
            crawl_delay = await self._robots.check(current)
            rule = self._rule.lowered_to(crawl_delay) if crawl_delay else self._rule
            # (3) in-run breaker.
            self._breaker.check(self.bucket_key)
            # (4) a lease. A retry re-acquires a token like any other request:
            #     backing off and then bursting is how a well-behaved client
            #     becomes a badly-behaved one.
            waited = await self._limiter.acquire(
                adapter=self.adapter,
                bucket_key=self.bucket_key,
                rule=rule,
                deadline_s=float(self._settings.rate_limit_wait_s),
            )
            self.counters.rate_limit_wait_ms += int(waited)

            request = self._client.build_request(
                method,
                current,
                params=params,
                headers=dict(headers) if headers else None,
                json=dict(json_body) if json_body is not None else None,
            )
            self.counters.requests += 1
            response = await self._client.send(request, stream=True, follow_redirects=False)

            if response.is_redirect:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise TransportError("redirect without a Location header")
                current = urljoin(str(response.url), location)
                # Loop: the next iteration re-runs the policy check *before*
                # the redirect target is contacted.
                continue

            try:
                body = await self._read_capped(response)
            finally:
                await response.aclose()
            return response.status_code, body, response.headers

        raise TransportError(f"more than {MAX_REDIRECTS} redirects")

    async def _read_capped(self, response: httpx.Response) -> bytes:
        """Read a response body, failing rather than truncating past the cap."""
        cap = self._settings.max_response_bytes
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > cap:
            raise ResponseTooLarge(f"content-length {declared} exceeds {cap} bytes")

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > cap:
                raise ResponseTooLarge(f"response body exceeded {cap} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _log(self, template: str, status: int, started: float, item_count: int | None) -> None:
        log.info(
            "source_fetch",
            source_id=self.source_id,
            adapter=self.adapter.value,
            url_template=template,
            status=status,
            duration_ms=int((self._monotonic() - started) * 1000),
            item_count=item_count,
        )


def _default_template(url: str) -> str:
    """Return a log-safe stand-in for a URL when the caller gave no template.

    Only the scheme and host survive: a path can carry a board token, and a
    board token in a log line is a leak.
    """
    parsed = httpx.URL(url)
    return f"{parsed.scheme}://{parsed.host}/…"


def build_source_client(
    client: httpx.AsyncClient,
    *,
    settings: Settings,
    source_id: int,
    adapter: AtsType,
    bucket_key: str | None,
    limiter: RateLimiter,
    robots: RobotsPolicy,
    breaker: InRunCircuitBreaker,
) -> SourceHttpClient:
    """Construct the facade one adapter will use for a run.

    Args:
        client: The run's shared httpx client.
        settings: Configuration.
        source_id: The source being fetched.
        adapter: Its adapter type.
        bucket_key: The rate-limiting domain; defaulted from
            ``STATIC_BUCKET_KEYS`` for adapters whose bucket is fixed.
        limiter: The token-bucket implementation.
        robots: The run's robots policy.
        breaker: The run's in-run circuit breaker.

    Returns:
        A ready ``SourceHttpClient``.

    Raises:
        ValueError: When the adapter has no fixed bucket and none was supplied.
    """
    key = bucket_key or STATIC_BUCKET_KEYS.get(adapter)
    if not key:
        raise ValueError(f"{adapter.value} has a per-tenant bucket; bucket_key is required")
    return SourceHttpClient(
        client,
        settings=settings,
        source_id=source_id,
        adapter=adapter,
        bucket_key=key,
        limiter=limiter,
        robots=robots,
        breaker=breaker,
    )


__all__ = [
    "BASE_DELAY_S",
    "LIMITS",
    "MAX_ATTEMPTS",
    "MAX_DELAY_S",
    "RATE_LIMITS",
    "RETRYABLE_EXC",
    "RETRYABLE_STATUS",
    "ROBOTS_FAIL_OPEN_HOSTS",
    "STATIC_BUCKET_KEYS",
    "TIMEOUT",
    "InRunCircuitBreaker",
    "NullRateLimiter",
    "RateLimitRule",
    "RateLimiter",
    "RedisTokenBucket",
    "RobotsPolicy",
    "SourceCounters",
    "SourceHttpClient",
    "backoff_delay",
    "build_client",
    "build_source_client",
    "parse_retry_after",
]
