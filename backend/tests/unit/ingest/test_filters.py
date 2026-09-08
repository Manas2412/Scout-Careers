"""Stage ④: what survives, what does not, and which reason is recorded.

The chain is ordered and first-rejection-wins, so "which reason" is part of the
contract rather than an implementation detail: it is the only explanation the
operator gets for a role they never saw.

Two of these tests exist because two documents disagree. Where that happens the
resolution belongs in a test, not in a memory — see the module docstring of
``ingest/filters.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scout_careers.common.types import CompanyStatus
from scout_careers.ingest.filters import (
    CHAIN,
    MIN_DESCRIPTION_CHARS,
    CompanyView,
    PostingView,
    evaluate,
    parse_experience_years,
    role_marker_hits,
)
from tests.conftest import make_settings

JD = "We are hiring a backend engineer. " * 40  # comfortably over the minimum


def body(text: str) -> str:
    """Pad a snippet past ``MIN_DESCRIPTION_CHARS``.

    The chain is ordered and `no_description` sits ahead of every predicate that
    reads the body, so a short fixture silently tests the wrong rule — it fails
    with `description_too_short`, or worse, passes for a reason the test never
    named. Anything asserting on `role_domain` or `experience` goes through here.
    """
    return f"{text} {JD}"


def posting(**overrides) -> PostingView:
    values = {
        "id": "01POSTING",
        "title": "Senior Backend Engineer",
        "description_text": JD,
        "location_city": "Bengaluru",
        "location_country": "IN",
        "seniority_guess": "senior",
    }
    values.update(overrides)
    return PostingView(**values)


def company(**overrides) -> CompanyView:
    values = {"id": 1, "slug": "acme"}
    values.update(overrides)
    return CompanyView(**values)


def verdict(post=None, comp=None, **setting_overrides):
    return evaluate(post or posting(), comp or company(), make_settings(**setting_overrides))


# --------------------------------------------------------------------------
# The happy path, so the rest means something
# --------------------------------------------------------------------------


def test_an_ordinary_indian_backend_role_survives() -> None:
    result = verdict()
    assert result.passed is True
    assert result.reason is None


# --------------------------------------------------------------------------
# Each predicate
# --------------------------------------------------------------------------


def test_a_blacklisted_company_is_dropped_whatever_the_role() -> None:
    assert verdict(comp=company(status=CompanyStatus.BLACKLISTED)).reason == "company_blacklisted"


def test_a_superseded_posting_is_not_extracted_twice() -> None:
    result = verdict(post=posting(filtered_out=True, filter_reason="superseded_by:01OTHER"))
    assert result.reason == "superseded"


def test_a_row_filtered_for_another_reason_is_not_treated_as_superseded() -> None:
    # `filtered_out` alone is not supersession; only the prefix is.
    result = verdict(post=posting(filtered_out=True, filter_reason="location"))
    assert result.passed is True


def test_a_closed_posting_is_dropped() -> None:
    assert verdict(post=posting(closed_at=datetime(2026, 9, 1, tzinfo=UTC))).reason == "closed"


def test_a_mail_alert_stub_never_reaches_the_extractor() -> None:
    """The predicate that protects the extractor from itself.

    Run over a stub the extractor does not fail — it invents. Confident, wrong
    requirements then produce a confident, wrong coverage score, and the
    operator cannot tell that from a real one by looking.
    """
    stub = posting(raw={"needs_description": True})
    assert verdict(post=stub).reason == "no_description"


def test_the_stub_gate_can_be_opened_deliberately() -> None:
    stub = posting(raw={"needs_description": True})
    assert verdict(post=stub, alert_fidelity_extract=True).passed is True


def test_a_short_description_is_treated_as_no_description() -> None:
    short = posting(description_text="x" * (MIN_DESCRIPTION_CHARS - 1))
    assert verdict(post=short).reason == "description_too_short"


@pytest.mark.parametrize(
    "band", ["intern", "staff", "principal", "manager", "director", "executive"]
)
def test_denied_seniority_bands_are_dropped(band: str) -> None:
    assert verdict(post=posting(seniority_guess=band)).reason == f"seniority:{band}"


@pytest.mark.parametrize("band", ["entry", "mid", "senior"])
def test_bands_being_applied_into_survive(band: str) -> None:
    assert verdict(post=posting(seniority_guess=band)).passed is True


def test_unknown_seniority_passes() -> None:
    """SDD.md §3.2 is explicit. The inference already resolves ambiguity to
    `mid`, so a residual `unknown` means the source stated nothing — which is
    not grounds for dropping a role."""
    assert verdict(post=posting(seniority_guess="unknown")).passed is True
    assert verdict(post=posting(seniority_guess=None)).passed is True


# --------------------------------------------------------------------------
# Location, which has the most rules
# --------------------------------------------------------------------------


def test_a_foreign_onsite_role_is_dropped_under_the_default_filter() -> None:
    us_role = posting(location_city="San Francisco", location_country="US", is_remote=False)
    assert verdict(post=us_role).reason == "location"


def test_remote_is_a_token_about_arrangement_not_geography() -> None:
    remote_us = posting(location_city="San Francisco", location_country="US", is_remote=True)
    assert verdict(post=remote_us).passed is True


def test_an_unresolvable_location_passes_a_non_empty_filter() -> None:
    """Deliberately asymmetric.

    A false positive costs two seconds of the operator's attention. A false
    negative is a role they never see and never learn they missed.
    """
    nowhere = posting(location_city=None, location_country=None, is_remote=False)
    assert verdict(post=nowhere).passed is True


def test_a_company_filter_overrides_the_global_default_entirely() -> None:
    # Not an intersection: "for this employer, only these places".
    pune_only = company(location_filter=["Pune"])
    assert verdict(post=posting(location_city="Pune"), comp=pune_only).passed is True
    assert verdict(post=posting(location_city="Bengaluru"), comp=pune_only).reason == "location"


def test_a_negative_token_vetoes_even_a_positive_match() -> None:
    no_chennai = company(location_filter=["IN", "!Chennai"])
    chennai = posting(location_city="Chennai", location_country="IN")
    assert verdict(post=chennai, comp=no_chennai).reason == "location_excluded:chennai"


def test_an_empty_filter_accepts_every_location() -> None:
    anywhere = posting(location_city="Reykjavik", location_country="IS")
    assert verdict(post=anywhere, default_location_filter=[]).passed is True


# --------------------------------------------------------------------------
# The keyword deny-list
# --------------------------------------------------------------------------


def test_the_shipped_deny_list_does_what_it_was_measured_to_do() -> None:
    """The one test here that deliberately reads the shipped default.

    Every other keyword test states its own list, because a test of the
    matching *mechanism* should not break when a *value* changes — that
    happened twice while tuning this list. But the shipped list is itself a
    decision, made against 7,200 real postings, and it deserves a test of its
    own: what it catches, and what it was deliberately not allowed to catch.
    """
    settings = make_settings()

    # Catches what it was added for. `account executive` was the largest single
    # win at 222 postings, and no engineering role was lost to it.
    assert verdict(post=posting(title="Enterprise Sales Engineer")).reason == "title_keyword:sales"
    assert verdict(post=posting(title="Strategic Account Executive, AI")).reason == (
        "title_keyword:account executive"
    )

    # Rejected entries, each for a measured reason. `firmware` and `rtos` would
    # have hidden the Seagate roles this project began with. `marketing` and
    # `audit` name an org rather than a role and cost one engineering job in
    # four. `solutions architect` and `strategist` each cost a role worth
    # seeing, including Palantir's Forward Deployed Strategist.
    for rejected in ("firmware", "rtos", "device driver", "marketing", "audit", "strategist"):
        assert rejected not in settings.filter_keyword_deny, (
            f"{rejected!r} is back on the shipped deny-list; it was excluded on evidence"
        )

    for title in (
        "Firmware Engineer, Storage",
        "Senior Software Engineer, Marketing Platform Tooling",
        "Full Stack Engineer - Internal Audit",
        "Forward Deployed Strategist",
        "Specialist Solutions Architect - AI/ML",
    ):
        assert verdict(post=posting(title=title)).passed is True, f"{title} was dropped"


def test_a_multi_word_entry_matches_as_a_phrase() -> None:
    """States its own deny-list rather than borrowing the shipped one.

    This read `device driver` out of the default and broke the moment that
    entry was removed from the default for good reasons — a test of the
    phrase-matching *mechanism* failing because a *configuration value*
    changed. A mechanism test that depends on today's settings will keep
    failing for reasons that have nothing to do with the mechanism.
    """
    result = verdict(
        post=posting(title="Device Driver Engineer"),
        filter_keyword_deny=["driver", "device driver"],
    )
    assert result.reason == "title_keyword:device driver"


def test_the_match_is_whole_word() -> None:
    """`sales` must not kill Salesforce, and `driver` must not kill Driverless.

    Substring matching here would be quietly destructive — nobody inspects the
    postings that were filtered out.
    """
    assert verdict(post=posting(title="Salesforce Platform Engineer")).passed is True
    assert verdict(post=posting(title="Driverless Systems Engineer")).passed is True


def test_the_deny_list_does_not_read_the_description() -> None:
    """The documented contradiction, pinned.

    SDD.md §3.2 has a predicate matching the deny-list over `description_text`;
    CONFIGURATION.md §8 — canonical for what a key means — describes
    FILTER_KEYWORD_DENY as title keywords. Title-only is also the safer of the
    two: `firmware` is on the default list, and a backend JD that mentions
    firmware once is not a firmware job. Description matching would have
    dropped the two Seagate roles this project began with.
    """
    mentions_firmware = posting(
        title="Backend Engineer",
        description_text=JD + " You will work alongside the firmware team. " * 5,
    )
    assert verdict(post=mentions_firmware).passed is True


# --------------------------------------------------------------------------
# Ordering is contract, not optimisation
# --------------------------------------------------------------------------


def test_the_first_rejection_wins() -> None:
    """A posting failing several predicates records the earliest one.

    Otherwise "why was this dropped" has as many answers as the chain has
    steps, and the reason column stops being usable as an explanation.
    """
    doomed = posting(
        title="Sales Director",  # title_denylist, last
        seniority_guess="director",  # seniority, middle
        closed_at=datetime(2026, 9, 1, tzinfo=UTC),  # closed, early
    )
    assert verdict(post=doomed, comp=company(status=CompanyStatus.BLACKLISTED)).reason == (
        "company_blacklisted"
    )
    assert verdict(post=doomed).reason == "closed"


def test_the_chain_is_ordered_cheapest_first() -> None:
    names = [name for name, _ in CHAIN]
    assert names.index("company_status") < names.index("no_description")
    assert names.index("no_description") < names.index("title_denylist")


def test_the_chain_is_exactly_what_it_should_be() -> None:
    """Names and order, not a count.

    This asserted `len(CHAIN) == 7` and broke the moment the `experience`
    predicate was added — telling us a number had changed but not which rule,
    which is the least useful thing a test can say. Naming them makes an
    accidental removal or reordering legible, and a deliberate addition a
    one-line edit that states what was added.
    """
    assert [name for name, _ in CHAIN] == [
        "company_status",
        "superseded",
        "closed",
        "no_description",
        "seniority",
        "location",
        "title_denylist",
        "role_domain",
        "experience",
    ]
    assert len(CHAIN) == len({name for name, _ in CHAIN}), "duplicate predicate name"


# --------------------------------------------------------------------------
# The property the whole stage exists for
# --------------------------------------------------------------------------


def test_the_filter_issues_no_model_calls() -> None:
    """Gate 2.6's second half, asserted structurally.

    ``filters.py`` must not import an LLM client, a provider, or anything that
    could reach a network. The gate says the filter issues zero model calls;
    this is what makes that a property of the code rather than of today's
    behaviour.
    """
    import scout_careers.ingest.filters as module

    source = module.__doc__ or ""
    del source
    imported = set(vars(module))
    forbidden = {"boto3", "httpx", "LLMClient", "BedrockProvider", "invoke_model"}
    assert imported & forbidden == set()


def test_the_most_specific_deny_entry_names_the_reason() -> None:
    """`driver` and `device driver` are both on the default list.

    In file order the shorter matches first, and "Device Driver Engineer" was
    reported as `title_keyword:driver` — dropped correctly, explained wrongly,
    reading as a delivery job. The reason column is the only account the
    operator gets of a posting they never saw.
    """
    result = verdict(
        post=posting(title="Device Driver Engineer"),
        filter_keyword_deny=["sales", "driver", "device driver"],
    )
    assert result.reason == "title_keyword:device driver"


def test_the_reason_does_not_depend_on_how_the_env_line_was_typed() -> None:
    """A total ordering, so two orderings of the same list behave identically."""
    forwards = verdict(
        post=posting(title="Device Driver Engineer"),
        filter_keyword_deny=["driver", "device driver"],
    )
    backwards = verdict(
        post=posting(title="Device Driver Engineer"),
        filter_keyword_deny=["device driver", "driver"],
    )
    assert forwards.reason == backwards.reason == "title_keyword:device driver"


def test_duplicate_and_untrimmed_entries_are_tolerated() -> None:
    result = verdict(
        post=posting(title="Enterprise Sales Engineer"),
        filter_keyword_deny=["  Sales ", "sales", "SALES"],
    )
    assert result.reason == "title_keyword:sales"


# --------------------------------------------------------------------------
# Years of experience, read from the description
#
# The predicate that reads what a role asks for instead of guessing from its
# name. A title deny-list simulation lost 14 real engineering roles —
# "Specialist Solutions Architect - AI/ML", "Full Stack Engineer - Internal
# Audit" — to crude word matching. None of those is a mistake this can make.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8+ years of professional software engineering experience", 8),
        ("Minimum 5 years of experience building distributed systems", 5),
        ("3-5 years of relevant industry experience", 3),
        ("12 to 15 years of professional experience", 12),
        ("2+ yrs experience with Python", 2),
        ("1+ year of experience", 1),
        ("At least 10 years experience in enterprise sales", 10),
    ],
)
def test_a_stated_requirement_is_read(text: str, expected: int) -> None:
    assert parse_experience_years(text) == expected


def test_the_least_demanding_figure_wins() -> None:
    """A JD asking 8 years overall and 3 with Go is a 3-year role for our purposes.

    Taking the maximum would drop roles the operator could plausibly get. Only
    when *every* stated figure is out of reach is the role out of reach, and
    that is a far safer claim than guessing which number is the headline.
    """
    jd = "We need 8+ years of engineering experience, and 3+ years of experience with Go."
    assert parse_experience_years(jd) == 3


def test_zero_is_an_answer_and_is_not_silence() -> None:
    """`0-2 years` is a new-graduate role — the opposite of an unstated one.

    An early version returned None here because the bound was `0 < n`, which
    recorded the most relevant roles in the corpus as "not stated".
    """
    assert parse_experience_years("BS/MS in CS with 0-2 years of experience") == 0
    assert parse_experience_years("Competitive salary. Unlimited PTO.") is None


@pytest.mark.parametrize(
    "text",
    [
        "401(k) matching and 15 years of company history",
        "Over the last 5 years we grew from 10 to 500 people",
        "Five years of experience",  # words, not digits: not inferred
        "Founded 20 years ago",
    ],
)
def test_a_number_of_years_that_is_not_a_requirement_is_ignored(text: str) -> None:
    """A bare "N years" is prose. One of the context words has to be near it.

    "Five years" returns None rather than five on purpose: a parser that starts
    interpreting is one that starts being wrong quietly, and an unstated
    requirement is kept rather than dropped.
    """
    assert parse_experience_years(text) is None


def test_a_role_beyond_reach_is_dropped_with_the_figure_in_the_reason() -> None:
    demanding = posting(description_text=JD + " Requires 10+ years of engineering experience.")
    assert verdict(post=demanding).reason == "experience:10y"


def test_a_role_within_reach_survives() -> None:
    reachable = posting(description_text=JD + " 2+ years of professional experience required.")
    assert verdict(post=reachable).passed is True


def test_a_description_stating_nothing_passes() -> None:
    """Same asymmetry as the location rule, for the same reason."""
    assert verdict(post=posting(description_text=JD)).passed is True


def test_the_ceiling_is_configurable_and_zero_disables_it() -> None:
    demanding = posting(description_text=JD + " Requires 10+ years of experience.")
    assert verdict(post=demanding, filter_max_years_experience=12).passed is True
    assert verdict(post=demanding, filter_max_years_experience=0).passed is True


def test_experience_runs_last_because_it_reads_the_whole_description() -> None:
    names = [name for name, _ in CHAIN]
    assert names[-1] == "experience"
    assert names.index("title_denylist") < names.index("experience")


def test_the_roles_a_title_denylist_would_have_wrongly_dropped_survive() -> None:
    """The 14 collateral losses from the deny-list simulation, by name.

    Each is a real engineering role that a word-matching deny-list removed for
    containing `marketing`, `audit` or `solutions architect`. Reading the stated
    requirement instead keeps every one of them.
    """
    junior_jd = JD + " Looking for 2+ years of relevant engineering experience."
    for title in (
        "Senior Fullstack Engineer, Marketing",
        "Full Stack Engineer - Internal Audit",
        "Specialist Solutions Architect - AI/ML",
        "Senior Software Engineer II, Marketing Enablement & Technology",
        "Forward Deployed AI Accelerator, Marketing",
    ):
        result = verdict(post=posting(title=title, description_text=junior_jd))
        assert result.passed is True, f"{title} was dropped for {result.reason}"


# --------------------------------------------------------------------------
# Domain: the predicate the title deny-list cannot replace
# --------------------------------------------------------------------------


# Padded past MIN_DESCRIPTION_CHARS with `JD`: the chain is ordered and
# `no_description` sits ahead of `role_domain`, so a short body would test the
# wrong predicate and pass for the wrong reason.
GTM_BODY = body(
    "You will own outbound pipeline generation, running prospecting sequences "
    "and discovery calls. Strong qualification instincts, able to assess ICP "
    "fit. Experience with Salesforce or similar GTM tooling. Carry a quota."
)


def test_a_go_to_market_advert_is_dropped_on_its_body() -> None:
    """Its title is "Deployment Strategist" or "Growth Lead" — nothing a title
    deny-list can match. Only the body says what the role is."""
    verdict = evaluate(
        posting(title="Deployment Strategist", description_text=GTM_BODY),
        company(),
        make_settings(),
    )
    assert not verdict.passed
    assert (verdict.reason or "").startswith("role_domain:")


def test_the_reason_names_the_markers_that_matched() -> None:
    """A rejection the operator cannot argue with is a rejection they cannot tune.

    The reason carries the terms, alphabetically so it is stable between runs,
    and the *count* of the ones it had no room for. Seven markers and exactly
    three would otherwise read identically — and that difference is the whole
    judgement when deciding whether `FILTER_ROLE_MARKER_MIN` is set right.
    """
    reason = (
        evaluate(
            posting(title="Growth Lead", description_text=GTM_BODY), company(), make_settings()
        ).reason
        or ""
    )
    assert reason.startswith("role_domain:")
    named = reason.removeprefix("role_domain:").split("+")
    assert "icp" in named and "pipeline generation" in named
    assert named[-1].endswith("more"), "the markers it could not name are still counted"


def test_a_reason_that_fits_carries_no_overflow_note() -> None:
    reason = (
        evaluate(
            posting(
                title="Growth Lead",
                description_text=body(
                    "You will carry a quota, run prospecting sequences, and own the sales cycle."
                ),
            ),
            company(),
            make_settings(),
        ).reason
        or ""
    )
    assert reason == "role_domain:prospecting+quota+sales cycle"


def test_one_incidental_marker_does_not_drop_an_engineering_role() -> None:
    """The threshold is above one on purpose. An engineering advert mentions a
    quota now and then, and a single accidental hit must not remove it."""
    assert evaluate(
        posting(
            title="Backend Engineer",
            description_text=body("You will work with the sales team on quota planning."),
        ),
        company(),
        make_settings(),
    ).passed


def test_a_forward_deployed_role_survives() -> None:
    """The role this predicate most has to not break.

    CONFIGURATION.md already records that `strategist` and `solutions architect`
    were kept out of the title deny-list because each cost a role worth seeing,
    and the operator named forward-deployed engineering as something they would
    apply for. These roles ask for Python and API integration; an account
    executive asks for Salesforce and a quota.
    """
    assert evaluate(
        posting(
            title="Forward Deployed Engineer",
            description_text=body(
                "Work with enterprise customers to deploy our AI platform. Basic "
                "proficiency in Python. Familiarity with API integration, sufficient "
                "to prototype and demo. Comfort engaging in deal conversations."
            ),
        ),
        company(),
        make_settings(),
    ).passed


def test_the_predicate_can_be_switched_off() -> None:
    assert evaluate(
        posting(title="Growth Lead", description_text=GTM_BODY),
        company(),
        make_settings(filter_role_marker_min=0),
    ).passed


# --------------------------------------------------------------------------
# role_marker_hits: one definition, two callers
# --------------------------------------------------------------------------


def test_the_hits_helper_and_the_predicate_agree() -> None:
    """The diagnostic and the filter must not drift apart.

    `filter markers` exists to tune `FILTER_ROLE_MARKER_MIN` against real
    counts. If it computed hits its own way, the tuning would eventually be
    done against a number the filter does not use — and the disagreement would
    surface as a threshold that behaves differently from the tool that chose it.
    """
    settings = make_settings()
    text = body("We run discovery calls, manage a quota and live in Salesforce.")
    hits = role_marker_hits(text, settings)
    assert set(hits) == {"discovery calls", "quota", "salesforce"}
    assert len(hits) >= settings.filter_role_marker_min
    verdict = evaluate(posting(description_text=text), company(), settings)
    assert not verdict.passed
    for marker in hits[:2]:
        assert marker in (verdict.reason or "")


def test_the_hits_helper_deduplicates() -> None:
    """Distinct markers, not occurrences. An advert repeating "quota" eight
    times states one thing about itself, and counting repetitions would let a
    single word cross a threshold built to need several independent signals."""
    settings = make_settings()
    text = body("quota quota quota quota quota")
    assert role_marker_hits(text, settings) == ("quota",)


def test_an_engineering_posting_hits_nothing() -> None:
    settings = make_settings()
    text = body("Build async Python services on Postgres and Redis behind an API.")
    assert role_marker_hits(text, settings) == ()


def test_commission_is_not_a_marker() -> None:
    """Removed on measurement, and pinned so it does not come back.

    It reads like a sales word and it is compensation boilerplate. Against the
    live corpus it matched roughly thirty genuine engineering roles — "Senior
    Software Engineer, Core Platform", "Senior Machine Learning Engineer",
    "Electrical Engineer, Actuator Test Infrastructure" — and caught nothing the
    rest of the list did not already catch, because every commissioned role also
    carries `quota` or `on-target earnings`.
    """
    settings = make_settings()
    assert "commission" not in settings.filter_role_marker_deny
    text = body("Compensation includes equity, an annual bonus and commission.")
    assert role_marker_hits(text, settings) == ()


def test_two_markers_now_reject() -> None:
    """The threshold moved from three to two after all 66 postings sitting at
    exactly two hits were read, and every one was go-to-market."""
    settings = make_settings()
    assert settings.filter_role_marker_min == 2
    text = body("You will own a quota and work the deal cycle end to end.")
    assert not evaluate(posting(description_text=text), company(), settings).passed


def test_one_marker_still_survives() -> None:
    """The rule that keeps "Engineering - Internal AI Transformation" — a
    posting currently ranked fourth — in the corpus. It mentions Salesforce
    once."""
    settings = make_settings()
    text = body("The platform integrates with Salesforce and a dozen other systems.")
    assert len(role_marker_hits(text, settings)) == 1
    assert evaluate(posting(description_text=text), company(), settings).passed


# --------------------------------------------------------------------------
# The title deny list, after `filter try-titles`
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Senior Payroll Accountant",
        "Litigation Paralegal",
        "Senior FP&A Analyst, Corporate Finance",
        "Strategic Finance, GTM",
        "Recruiting Coordinator (Contract)",
        "Executive Assistant, R&D",
        "People Analytics Lead - Recruiting",
        "Brand Designer, Creative Studio",
        "Lead Product Designer, Growth",
        "ASIC Package SI/PI Engineer",
        "Data Center Supply Planning Lead",
        "Power Trading Lead",
    ],
)
def test_a_whole_function_is_denied_by_title(title: str) -> None:
    """Every one of these survived the go-to-market markers with zero hits.

    They are catchable by title where the GTM roles were not — "Deployment
    Strategist" contains no denied word, "Litigation Paralegal" is nothing but
    denied words. Two different problems, two different predicates.
    """
    verdict = evaluate(posting(title=title), company(), make_settings())
    assert not verdict.passed
    # `title_keyword:<entry>`, not the chain's own name for the predicate. The
    # reason column names the matched word, which is what makes a rejection
    # arguable — the operator can see which entry did it.
    assert (verdict.reason or "").startswith("title_keyword:")


@pytest.mark.parametrize(
    "title",
    [
        "Full Stack Engineer - Internal Audit",
        "Senior Software Engineer, Workers Runtime",
        "Backend Engineer, Geo Team",
        "Forward Deployed Engineer",
        "Deployment Strategist",
        "Software Engineer, R2 Gateway",
    ],
)
def test_the_denied_functions_do_not_reach_engineering_titles(title: str) -> None:
    """The guard on the whole exercise.

    `internal audit` was simulated and rejected because it matched the first of
    these — the same failure that got `audit` rejected before it. The last two
    are pre-sales roles left deliberately alone: CONFIGURATION.md records that
    the operator named forward-deployed engineering as something they would
    apply for, and a title rule reaching them would cost exactly that.
    """
    assert evaluate(posting(title=title), company(), make_settings()).passed


def test_no_deny_entry_is_dead() -> None:
    """`copywriter` and `datacenter` were dropped for matching nothing.

    An entry that never fires is indistinguishable from one that is wrong, and
    it makes a hand-edited list longer to read for no removal. This cannot check
    the live corpus, so it checks the weaker thing that still catches a paste
    error: that no entry is empty or duplicated.
    """
    entries = make_settings().filter_keyword_deny
    assert all(entry.strip() for entry in entries)
    assert len(set(entries)) == len(entries)
