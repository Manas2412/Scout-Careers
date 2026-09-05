"""Invariant 4: the never-scrape list is absolute.

This is the most important test file in the repository. Everything else here
degrades a capability when it breaks; this one is the difference between a
system that does what it says and one that does not.

What is asserted:

1. Every listed host is refused, and so is every subdomain of one.
2. A URL that redirects to a listed host is refused *before* the redirect is
   followed, so the denied host is never contacted.
3. The deny list is not reachable from ``Settings`` — no field names it, no
   field value contains one of its hosts, and it is not an attribute of the
   settings class.
4. ``assert_fetch_allowed`` has exactly one parameter and no way to be told not
   to check.
5. The constant is immutable.
"""

from __future__ import annotations

import ast
import inspect

import httpx
import pytest
import respx

from scout_careers.common import config as config_module
from scout_careers.common.config import Settings
from scout_careers.common.errors import DeniedByPolicy
from scout_careers.common.types import AtsType
from scout_careers.sources import policy
from scout_careers.sources.http import (
    InRunCircuitBreaker,
    NullRateLimiter,
    RobotsPolicy,
    SourceHttpClient,
    build_client,
)
from scout_careers.sources.policy import NEVER_FETCH_HOSTS, assert_fetch_allowed, is_denied_host
from tests.conftest import make_settings

ALLOWED_URLS = [
    "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true",
    "https://api.lever.co/v0/postings/netflix?mode=json",
    "https://api.ashbyhq.com/posting-api/job-board/openai",
    "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced/jobs",
    "http://careers.example.com/jobs",
    # Near-misses that must NOT be denied: the suffix rule is on a dot boundary.
    "https://notlinkedin.com/jobs",
    "https://mylinkedin.com/x",
    "https://indeed.com.example.org/x",
]


# --------------------------------------------------------------------------
# 1. Every listed host, and every subdomain of one
# --------------------------------------------------------------------------


@pytest.mark.parametrize("host", sorted(NEVER_FETCH_HOSTS))
def test_every_listed_host_is_refused(host: str) -> None:
    with pytest.raises(DeniedByPolicy) as excinfo:
        assert_fetch_allowed(f"https://{host}/jobs/search?q=engineer")
    assert excinfo.value.host == host
    assert excinfo.value.error_code == "source.denied_by_policy"


@pytest.mark.parametrize("host", sorted(NEVER_FETCH_HOSTS))
def test_subdomains_of_listed_hosts_are_refused(host: str) -> None:
    for prefix in ("jobs", "api.careers", "a.b.c"):
        with pytest.raises(DeniedByPolicy):
            assert_fetch_allowed(f"https://{prefix}.{host}/anything")


def test_case_and_trailing_dot_do_not_evade() -> None:
    for url in (
        "https://WWW.LinkedIn.COM/jobs",
        "https://www.linkedin.com./jobs",
        "https://Jobs.NAUKRI.com/x",
    ):
        with pytest.raises(DeniedByPolicy):
            assert_fetch_allowed(url)


def test_userinfo_and_port_do_not_evade() -> None:
    for url in (
        "https://user:pass@www.linkedin.com/jobs",
        "https://www.linkedin.com:8443/jobs",
    ):
        with pytest.raises(DeniedByPolicy):
            assert_fetch_allowed(url)


@pytest.mark.parametrize("url", ALLOWED_URLS)
def test_allowed_urls_pass(url: str) -> None:
    assert_fetch_allowed(url)


def test_hostless_url_fails_closed() -> None:
    # An unparseable URL is not evidence of permission.
    for url in ("", "not-a-url", "file:///etc/passwd", "https:///jobs"):
        with pytest.raises(DeniedByPolicy):
            assert_fetch_allowed(url)


def test_malformed_url_that_httpx_itself_rejects_fails_closed() -> None:
    # httpx raises before a host can be extracted at all. Fail closed: a URL
    # nobody can parse is not a URL anybody has cleared.
    for url in ("http://[::1", "http://\udcff/jobs", "https://" + "a" * 70_000 + ".com"):
        with pytest.raises(DeniedByPolicy) as excinfo:
            assert_fetch_allowed(url)
        assert excinfo.value.host == "<unparseable>"


def test_is_denied_host_branches() -> None:
    assert is_denied_host("linkedin.com") is True  # exact
    assert is_denied_host("jobs.linkedin.com") is True  # suffix
    assert is_denied_host("") is True  # fail closed
    assert is_denied_host("boards-api.greenhouse.io") is False


# --------------------------------------------------------------------------
# 2. A redirect to a denied host is refused before it is followed
# --------------------------------------------------------------------------


def _client(settings, http: httpx.AsyncClient) -> SourceHttpClient:
    return SourceHttpClient(
        http,
        settings=settings,
        source_id=1,
        adapter=AtsType.GREENHOUSE,
        bucket_key="boards-api.greenhouse.io",
        limiter=NullRateLimiter(),
        robots=RobotsPolicy(http, user_agent=settings.source_user_agent, cache_ttl_s=60),
        breaker=InRunCircuitBreaker(threshold=settings.circuit_breaker_failures),
    )


@respx.mock
async def test_redirect_to_denied_host_is_refused(settings) -> None:
    respx.get("https://boards-api.greenhouse.io/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    origin = respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(302, headers={"location": "https://www.linkedin.com/jobs/acme"})
    )
    denied = respx.get("https://www.linkedin.com/jobs/acme").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    async with build_client(settings) as http:
        client = _client(settings, http)
        with pytest.raises(DeniedByPolicy) as excinfo:
            await client.get_json("https://boards-api.greenhouse.io/v1/boards/acme/jobs")

    assert excinfo.value.host == "www.linkedin.com"
    assert origin.called
    # The whole point: the denied host was never contacted.
    assert not denied.called


@respx.mock
async def test_redirect_to_a_permitted_host_is_followed(settings) -> None:
    respx.get("https://boards-api.greenhouse.io/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://boards.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(301, headers={"location": "https://boards.example.com/j"})
    )
    respx.get("https://boards.example.com/j").mock(
        return_value=httpx.Response(200, json={"jobs": [1, 2]})
    )

    async with build_client(settings) as http:
        payload = await _client(settings, http).get_json(
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        )
    assert payload == {"jobs": [1, 2]}


# --------------------------------------------------------------------------
# 3. The deny list is not reachable from Settings
# --------------------------------------------------------------------------


#: Settings fields whose names contain a deny-ish word but which have nothing to
#: do with invariant 4. Each is named individually, with its reason, so that a
#: genuinely new host deny-list still trips the scan below and has to be argued
#: for rather than quietly absorbed by a looser pattern.
#:
#: Invariant 4 is about *which hosts may be fetched*. These two are about which
#: job postings are worth a model call — a preference the operator is supposed
#: to tune, on a list whose worst outcome is a role they have to find manually.
#: Nothing here can widen what the crawler is allowed to touch.
DENY_NAMED_BUT_NOT_HOSTS: frozenset[str] = frozenset(
    {
        "filter_seniority_deny",
        "filter_keyword_deny",
    }
)


def test_no_settings_field_names_the_deny_list() -> None:
    suspicious = ("never_fetch", "deny", "denied", "blocklist", "blacklist", "allow_host")
    for field_name in Settings.model_fields:
        if field_name in DENY_NAMED_BUT_NOT_HOSTS:
            continue
        lowered = field_name.lower()
        assert not any(token in lowered for token in suspicious), (
            f"Settings.{field_name} looks like a configurable deny list; "
            "invariant 4 requires the list to be a code constant"
        )


def test_the_exemptions_still_exist_and_still_hold_no_hosts() -> None:
    """An exemption is a hole. Two things must stay true of each one.

    It must name a field that exists — a stale entry is a hole with nothing
    behind it, waiting to match a future field of the same name. And its value
    must contain nothing host-shaped, because the exemption is granted on the
    claim that these lists are about job titles and seniority bands, not about
    what may be fetched.
    """
    settings = make_settings()
    stale = sorted(DENY_NAMED_BUT_NOT_HOSTS - set(Settings.model_fields))
    assert stale == [], f"exemptions for fields that no longer exist: {stale}"

    for field_name in DENY_NAMED_BUT_NOT_HOSTS:
        for entry in getattr(settings, field_name):
            assert "." not in entry and "/" not in entry, (
                f"Settings.{field_name} contains {entry!r}, which looks like a host. "
                "This field is exempt from the invariant-4 scan on the basis that "
                "it holds titles and bands; a host here voids that basis."
            )


def test_no_settings_value_carries_a_denied_host() -> None:
    settings = make_settings()
    for field_name in Settings.model_fields:
        value = getattr(settings, field_name)
        rendered = str(value).lower()
        for host in NEVER_FETCH_HOSTS:
            assert host not in rendered, f"Settings.{field_name} references {host}"


def test_deny_list_is_not_an_attribute_of_settings_or_its_module() -> None:
    assert not hasattr(Settings, "NEVER_FETCH_HOSTS")
    assert not hasattr(Settings, "never_fetch_hosts")
    assert not hasattr(config_module, "NEVER_FETCH_HOSTS")


def test_config_module_does_not_import_the_policy_module() -> None:
    # Checked on the AST, not the text: config.py's docstring is allowed to
    # explain why the list lives elsewhere, but no code may reach it.
    tree = ast.parse(inspect.getsource(config_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "sources.policy" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            assert "sources.policy" not in (node.module or "")
        elif isinstance(node, ast.Name):
            assert node.id != "NEVER_FETCH_HOSTS"
        elif isinstance(node, ast.Attribute):
            assert node.attr != "NEVER_FETCH_HOSTS"


# --------------------------------------------------------------------------
# 4. There is no parameter that disables the check
# --------------------------------------------------------------------------


def test_assert_fetch_allowed_takes_exactly_one_parameter() -> None:
    signature = inspect.signature(assert_fetch_allowed)
    assert list(signature.parameters) == ["url"]
    parameter = signature.parameters["url"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_policy_module_exposes_no_override() -> None:
    exported = {name for name in dir(policy) if not name.startswith("_")}
    forbidden = {"force", "override", "bypass", "allow_denied", "skip_policy", "disable"}
    assert not (exported & forbidden)


def test_policy_module_reads_no_configuration() -> None:
    # On the AST: the docstring may name Settings to explain why the list is not
    # one, but no code in policy.py may read configuration of any kind.
    tree = ast.parse(inspect.getsource(policy))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Name):
            assert node.id not in {"getenv", "environ", "Settings", "get_settings"}
        elif isinstance(node, ast.Attribute):
            assert node.attr not in {"getenv", "environ"}
    assert imported == {"httpx", "scout_careers.common.errors", "__future__"}


# --------------------------------------------------------------------------
# 5. The constant is immutable
# --------------------------------------------------------------------------


def test_deny_list_is_a_frozenset_and_cannot_be_mutated() -> None:
    assert isinstance(NEVER_FETCH_HOSTS, frozenset)
    assert not hasattr(NEVER_FETCH_HOSTS, "add")
    assert not hasattr(NEVER_FETCH_HOSTS, "discard")


def test_deny_list_content_is_the_documented_one() -> None:
    # A shrinking deny list must be a deliberate, visible diff.
    assert (
        frozenset(
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
        == NEVER_FETCH_HOSTS
    )
