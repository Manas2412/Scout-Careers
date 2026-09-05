"""The Google-backed transport: credentials, refresh, and two API calls.

Everything that needs a Google library lives here and nowhere else, which is
what lets :mod:`scout_careers.mail.gmail` — the module with all the behaviour —
be tested with no Google library and no socket.

Two things this module is careful about:

- **Refresh is serialised by a Redis lock** (``lock:gmail:refresh``). A poll and
  a manually triggered run refreshing the same grant at the same moment can
  invalidate each other's access token; the loser then retries, and on a bad day
  the pair alternate indefinitely. The lock holder re-reads the store *after*
  taking the lock, so the common case — somebody else already refreshed while we
  waited — costs one file read and no Google call at all.
- **``invalid_grant`` is permanent and is never retried.** It means the operator
  revoked access, changed their password, deleted the client, or left the token
  unused for six months. Retrying a permanent failure only burns quota
  (EMAIL_INGESTION.md §2.5). It is raised as
  :class:`~scout_careers.common.errors.GmailAuthError` and the run stops.

Nothing here logs a token, an access token, a prefix, a length or a hash. The
refresh log line is ``{"gmail_auth": "refreshed", "expires_in_s": 3599}`` and
nothing more (SECURITY_ARCHITECTURE.md §7.2 rule 7).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, ClassVar, Final, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from scout_careers.common.clock import utcnow
from scout_careers.common.config import Settings
from scout_careers.common.errors import (
    GmailApiError,
    GmailApiNotEnabled,
    GmailAuthError,
    GmailScopeInsufficient,
    MailNotConfigured,
)
from scout_careers.common.logging import get_logger
from scout_careers.mail.oauth import credentials_from_token, token_from_credentials
from scout_careers.mail.tokens import OAuthToken, TokenStore

log = get_logger(__name__)

#: Serialises refresh across every caller in the system.
GMAIL_REFRESH_LOCK_KEY: Final[str] = "lock:gmail:refresh"

#: Refresh this far ahead of expiry, so a request never races its own token's
#: last second (EMAIL_INGESTION.md §2.5).
REFRESH_SKEW_S: Final[int] = 300

#: How long a waiter will block for the lock holder to finish. A refresh is one
#: HTTP round trip; anything past this is a holder that died, and its TTL will
#: clear the key.
LOCK_WAIT_S: Final[float] = 10.0

#: Poll interval while waiting for the lock.
LOCK_POLL_S: Final[float] = 0.1

#: Gmail's own name for the authenticated mailbox.
USER_ID: Final[str] = "me"

HTTP_UNAUTHORIZED: Final[int] = 401
HTTP_FORBIDDEN: Final[int] = 403

#: Google's ``reason`` tokens for "the Gmail API is switched off on this Cloud
#: project". Both spellings are live: the older ``accessNotConfigured`` and the
#: newer ``SERVICE_DISABLED``. Matched, never quoted — see :meth:`_execute`.
_REASONS_API_DISABLED: Final[frozenset[str]] = frozenset(
    {"accessnotconfigured", "service_disabled", "servicedisabled"}
)

#: ``reason`` tokens for a valid credential that was granted too little.
_REASONS_SCOPE: Final[frozenset[str]] = frozenset(
    {"insufficientpermissions", "access_token_scope_insufficient", "forbidden"}
)


class RefreshLock(Protocol):
    """A mutually exclusive hold on the token refresh."""

    async def acquire(self) -> bool:
        """Try to take the lock, waiting briefly for a current holder."""

    async def release(self) -> None:
        """Release the lock if this holder still owns it."""


class NullRefreshLock:
    """No serialisation. Correct for a one-shot CLI invocation, and only there.

    A single process running a single command cannot race itself. The scheduler
    always passes a real Redis lock, because it can.
    """

    async def acquire(self) -> bool:
        """Always succeed."""
        return True

    async def release(self) -> None:
        """Nothing to release."""


class RedisRefreshLock:
    """``lock:gmail:refresh``, held for ``GMAIL_REFRESH_LOCK_TTL_S``.

    Unlike the discovery run lock, a waiter here *waits*: the second caller
    wants the refreshed token, not a refusal. It waits briefly and then proceeds
    anyway — an unserialised refresh is a small risk, and a mail run that blocks
    forever on a dead lock holder is a large one.
    """

    RELEASE_SCRIPT: ClassVar[str] = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    def __init__(self, redis: Redis, *, ttl_s: int, key: str = GMAIL_REFRESH_LOCK_KEY) -> None:
        self._redis = redis
        self._ttl_s = ttl_s
        self._key = key
        self._token = uuid.uuid4().hex
        self._held = False

    async def acquire(self) -> bool:
        """Take the lock, waiting up to :data:`LOCK_WAIT_S` for a current holder.

        Returns:
            True when this process took it. False when the wait elapsed, or when
            Redis is unreachable — the caller proceeds unserialised rather than
            failing a mail read over a lock.
        """
        deadline = asyncio.get_running_loop().time() + LOCK_WAIT_S
        while True:
            try:
                acquired = await self._redis.set(self._key, self._token, nx=True, ex=self._ttl_s)
            except RedisError:
                log.warning("gmail_refresh_lock_unavailable", key=self._key)
                return False
            if acquired:
                self._held = True
                return True
            if asyncio.get_running_loop().time() >= deadline:
                log.warning("gmail_refresh_lock_wait_expired", key=self._key)
                return False
            await asyncio.sleep(LOCK_POLL_S)

    async def release(self) -> None:
        """Release the lock, tolerating a Redis that went away."""
        if not self._held:
            return
        self._held = False
        try:
            await self._redis.eval(self.RELEASE_SCRIPT, 1, self._key, self._token)
        except RedisError:
            log.warning("gmail_refresh_lock_release_failed", key=self._key)


class GoogleGmailTransport:
    """``users.messages.list`` and ``users.messages.get``, and nothing else.

    Args:
        store: The encrypted token store.
        lock: Serialises refresh.
        static_discovery: Build the service from the bundled discovery document
            rather than fetching one. ``True`` in operation: a discovery fetch
            is a network call that can fail for reasons that have nothing to do
            with the mailbox.
    """

    def __init__(
        self,
        *,
        store: TokenStore,
        lock: RefreshLock,
        static_discovery: bool = True,
    ) -> None:
        self._store = store
        self._lock = lock
        self._static_discovery = static_discovery
        self._service: Any = None
        self._service_token: OAuthToken | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, *, redis: Redis | None = None
    ) -> GoogleGmailTransport:
        """Build the transport the running configuration describes.

        Args:
            settings: Configuration.
            redis: The client whose lock serialises refresh.

        Returns:
            A transport.

        Raises:
            MailNotConfigured: When no encryption key is configured, so no token
                could be read even if one existed.
        """
        store = TokenStore.from_settings(settings)
        if not store.is_encryptable:
            raise MailNotConfigured("MAIL_TOKEN_KEY is not set; the Gmail token cannot be read")
        lock: RefreshLock = (
            RedisRefreshLock(redis, ttl_s=settings.gmail_refresh_lock_ttl_s)
            if redis is not None
            else NullRefreshLock()
        )
        return cls(store=store, lock=lock)

    # -- GmailTransport ----------------------------------------------------

    async def list_messages(self, *, query: str, max_results: int) -> Mapping[str, Any]:
        """Return the raw ``users.messages.list`` response.

        Args:
            query: A Gmail search query.
            max_results: Page size ceiling.

        Returns:
            The decoded response.

        Raises:
            GmailAuthError: When the grant is no longer valid.
        """
        service = await self._ensure_service()

        def _call() -> Mapping[str, Any]:
            request = (
                service.users().messages().list(userId=USER_ID, q=query, maxResults=max_results)
            )
            result: Mapping[str, Any] = request.execute()
            return result

        return await self._execute(_call)

    async def get_message(self, message_id: str) -> Mapping[str, Any]:
        """Return the raw ``users.messages.get`` resource.

        Args:
            message_id: The Gmail message id.

        Returns:
            The full message resource, including the MIME payload.

        Raises:
            GmailAuthError: When the grant is no longer valid.
        """
        service = await self._ensure_service()

        def _call() -> Mapping[str, Any]:
            request = service.users().messages().get(userId=USER_ID, id=message_id, format="full")
            result: Mapping[str, Any] = request.execute()
            return result

        return await self._execute(_call)

    async def aclose(self) -> None:
        """Drop the built service. The token store owns nothing to close."""
        self._service = None
        self._service_token = None

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _reason_of(exc: Any) -> str:
        """Return Google's machine ``reason`` token for an ``HttpError``, lowercased.

        Args:
            exc: The ``googleapiclient.errors.HttpError``.

        Returns:
            The token, or ``""`` when the body is absent, not JSON, or shaped in
            some way this does not recognise.

        The value is only ever compared against the frozensets above. It is
        never logged, never raised and never shown, so an unexpected body cannot
        put upstream text into a log line — it can only fail to match, which
        lands on the generic branch, which is the same place an unparseable body
        lands. Both response shapes Google uses are read: the legacy
        ``error.errors[].reason`` and the current ``error.status``.
        """
        raw = getattr(exc, "content", None)
        if not raw:
            return ""
        try:
            body = json.loads(raw)
            error = body["error"]
            for entry in error.get("errors") or ():
                reason = entry.get("reason")
                if isinstance(reason, str) and reason:
                    return reason.strip().lower()
            status = error.get("status")
            return status.strip().lower() if isinstance(status, str) else ""
        except (ValueError, TypeError, AttributeError, KeyError, IndexError):
            return ""

    async def _execute(self, call: Any) -> Mapping[str, Any]:
        """Run one blocking API call off the event loop, classifying the failure.

        Every non-401 status used to become a :class:`GmailAuthError`, whose code
        is ``mail.auth_invalid_grant``. A live run met ``403 accessNotConfigured``
        — the Gmail API not switched on for the Cloud project — and was told its
        grant was invalid. Re-authorising fixes nothing there, and succeeds every
        time, so the advice is a loop.

        The status alone cannot separate those cases: 403 is both "the API is
        off" and "your token lacks the scope". Google's ``reason`` token does,
        and it is matched against :data:`_REASONS_API_DISABLED` and
        :data:`_REASONS_SCOPE` rather than read out, so nothing from the upstream
        body crosses this boundary — a reason we do not recognise falls through
        to the generic branch exactly as an unparseable one would.
        """
        from googleapiclient.errors import HttpError

        try:
            result: Mapping[str, Any] = await asyncio.to_thread(call)
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == HTTP_UNAUTHORIZED:
                raise GmailAuthError(
                    "Gmail refused the stored credential; re-authorise with "
                    "`scout-careers auth gmail`"
                ) from None
            if status == HTTP_FORBIDDEN:
                reason = self._reason_of(exc)
                if reason in _REASONS_API_DISABLED:
                    raise GmailApiNotEnabled(
                        "the Gmail API is not enabled on this Google Cloud project. "
                        "Enable it at console.cloud.google.com under APIs & Services "
                        "> Library > Gmail API. The stored credential is fine and "
                        "re-authorising will not help."
                    ) from None
                if reason in _REASONS_SCOPE:
                    raise GmailScopeInsufficient(
                        "the stored credential lacks the scope this call needs; "
                        "re-authorise with `scout-careers auth gmail`"
                    ) from None
            # The upstream body is untrusted and may quote the request. Only the
            # status crosses this boundary.
            raise GmailApiError(
                f"the Gmail API returned HTTP {status}", status_code=status
            ) from None
        return result

    async def _ensure_service(self) -> Any:
        """Return a Gmail service bound to a currently-valid access token."""
        token = await self._fresh_token()
        if self._service is None or self._service_token is not token:
            from googleapiclient.discovery import build

            self._service = build(
                "gmail",
                "v1",
                credentials=credentials_from_token(token),
                cache_discovery=False,
                static_discovery=self._static_discovery,
            )
            self._service_token = token
        return self._service

    async def _fresh_token(self) -> OAuthToken:
        """Load the token, refreshing it under the lock when it is close to expiry."""
        token = self._store.load()
        if not _needs_refresh(token):
            return token

        acquired = await self._lock.acquire()
        try:
            # Re-read: the holder we waited behind has already done the work in
            # the common case, and a second refresh would invalidate their token.
            token = self._store.load()
            if not _needs_refresh(token):
                return token
            token = await self._refresh(token)
        finally:
            if acquired:
                await self._lock.release()
        return token

    async def _refresh(self, token: OAuthToken) -> OAuthToken:
        """Exchange the refresh token for a new access token and persist it.

        Args:
            token: The current credential.

        Returns:
            The refreshed credential.

        Raises:
            GmailAuthError: On ``invalid_grant`` or any other refresh failure.
                Permanent; never retried.
        """
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request

        credentials = credentials_from_token(token)
        try:
            await asyncio.to_thread(credentials.refresh, Request())
        except RefreshError:
            # `from None`: the library's message quotes Google's error body.
            raise GmailAuthError(
                "the Gmail refresh token is no longer valid (invalid_grant). "
                "Re-authorise with `scout-careers auth gmail`."
            ) from None

        refreshed = token_from_credentials(credentials)
        self._store.save(refreshed)
        expires_in = (
            int((refreshed.expiry - utcnow()).total_seconds()) if refreshed.expiry else None
        )
        log.info("gmail_auth", gmail_auth="refreshed", expires_in_s=expires_in)
        return refreshed


def _needs_refresh(token: OAuthToken, *, skew_s: int = REFRESH_SKEW_S) -> bool:
    """Report whether the access token is missing or close enough to expiry to renew.

    Args:
        token: The credential.
        skew_s: How far ahead of expiry to renew.

    Returns:
        True when a refresh is due.
    """
    if token.access_token is None or token.expiry is None:
        return True
    return token.expiry - timedelta(seconds=skew_s) <= utcnow()


__all__ = [
    "GMAIL_REFRESH_LOCK_KEY",
    "LOCK_WAIT_S",
    "REFRESH_SKEW_S",
    "GoogleGmailTransport",
    "NullRefreshLock",
    "RedisRefreshLock",
    "RefreshLock",
]
