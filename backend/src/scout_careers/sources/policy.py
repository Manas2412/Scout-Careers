"""The never-scrape list. Invariant 4, expressed as a frozen code constant.

Deliberate properties of this module, each of which has a test:

- ``NEVER_FETCH_HOSTS`` is a module-level ``frozenset``, not a ``Settings``
  field, not a database row and not a file read at boot. There is no
  environment variable, argument, keyword or admin toggle that disables the
  check, because an invariant that has somewhere to be switched off is not an
  invariant.
- ``assert_fetch_allowed`` takes exactly one parameter. Adding a ``force`` or
  ``allow_denied`` parameter would be the whole failure mode in one commit.
- It fails closed on a URL it cannot resolve to a host. An unparseable URL is
  not evidence of permission.

The check runs inside ``SourceHttpClient`` on every request and again on every
redirect hop, so a source that 302s to LinkedIn is refused mid-flight
(SOURCE_ADAPTERS.md §4.7).
"""

from __future__ import annotations

import httpx

from scout_careers.common.errors import DeniedByPolicy

NEVER_FETCH_HOSTS: frozenset[str] = frozenset(
    {
        "linkedin.com",
        "www.linkedin.com",
        "in.linkedin.com",
        "naukri.com",
        "www.naukri.com",
        "indeed.com",
        "in.indeed.com",
        "www.indeed.com",
        "glassdoor.com",
        "www.glassdoor.co.in",
        "monsterindia.com",
        "shine.com",
        "instahyre.com",
        "angel.co",
        "wellfound.com",
        "facebook.com",
        "www.facebook.com",
    }
)


def is_denied_host(host: str) -> bool:
    """Report whether a hostname is on the never-scrape list.

    Args:
        host: A hostname, with or without case normalisation.

    Returns:
        True when the host is listed, or is a subdomain of a listed host.
    """
    lowered = host.lower().rstrip(".")
    if not lowered:
        return True
    if lowered in NEVER_FETCH_HOSTS:
        return True
    return any(lowered.endswith("." + denied) for denied in NEVER_FETCH_HOSTS)


def assert_fetch_allowed(url: str) -> None:
    """Refuse to fetch a URL whose host is on the never-scrape list.

    Args:
        url: An absolute URL, already resolved through any redirect.

    Raises:
        DeniedByPolicy: When the host is listed, is a subdomain of a listed
            host, or cannot be determined from ``url``.
    """
    try:
        host = httpx.URL(url).host
    except (httpx.InvalidURL, ValueError) as exc:
        raise DeniedByPolicy("<unparseable>") from exc

    if is_denied_host(host):
        raise DeniedByPolicy(host.lower() or "<no-host>")


__all__ = ["NEVER_FETCH_HOSTS", "assert_fetch_allowed", "is_denied_host"]
