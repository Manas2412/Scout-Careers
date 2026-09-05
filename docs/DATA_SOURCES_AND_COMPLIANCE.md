# DATA SOURCES AND COMPLIANCE — Scout Careers

**Status:** as-designed (pre-implementation)
**Version:** 1.0 · 5 September 2026
**Authority:** canonical for the legal and ethical basis of every data source,
the never-scrape deny list and its reasoning, robots and user-agent policy,
per-source rate limits, the exclusion of automated submission and automated
outbound mail, and personal-data handling. `ARCHITECTURE.md` wins on the
invariants themselves; `SOURCE_ADAPTERS.md` wins on adapter mechanics and the
`bucket_key` implementation; `SECURITY_ARCHITECTURE.md` wins on how a control is
technically enforced. **Whether a source may be touched at all is decided here.**

This document is written by a practitioner, not a lawyer, and it is not legal
advice. It records the reasoning behind a set of deliberately conservative
engineering decisions, and where it summarises a law or a court decision it does
so to explain a design choice rather than to state a legal conclusion.

---

## 1. Purpose

### 1.1 Why this document exists before any code

Every automated job-search tool ever built has faced the same fork, and almost
all of them took the wrong branch: scrape the aggregators, mass-submit
applications, blast cold email to scraped recruiter addresses. It is the obvious
design. It is also the design that gets the applicant's LinkedIn account
terminated, gets them flagged as a spam applicant on the shared ATS platforms
that thousands of employers use, and gets their email domain reputation ruined —
all in service of an approach that converts worse than doing less, more
carefully.

That fork is a **design constraint**, not a compliance review to be conducted
after the fact. A boundary discovered late is a boundary that has already been
crossed by code that now has to be unwound, and by an operator who has already
sent the requests. So the boundary is written down first, expressed as invariants
in `ARCHITECTURE.md` §3, and enforced as absences: a deny list that is a frozen
code constant, an API with no submission endpoint, a mail path with one hardcoded
recipient.

The three invariants this document is the reasoning behind:

> **1. No automated submission.** The system never POSTs an application to an
> employer system, never drives a browser to submit a form, and never completes a
> CAPTCHA. It prepares; the human submits.
>
> **2. No automated outbound mail to people.** The system sends exactly one class
> of email — the daily digest, to the operator's own address. It never emails a
> recruiter, hiring manager or any third party.
>
> **4. The never-scrape list is absolute.** Sources on the deny list — LinkedIn
> first among them — are never fetched programmatically under any configuration.
> The list is a code constant, not a config value.

### 1.2 The four principles

1. **Use the channel the counterparty built for this purpose.** An employer's ATS
   publishes a public JSON endpoint because its own careers page consumes it.
   LinkedIn sends job alerts because it wants people to read them. Both are
   invitations. Neither requires anything to be defeated.
2. **Never defeat a control.** A login wall, a CSRF token, a rotating persisted
   query hash, TLS fingerprinting, a CAPTCHA and a 403 to an honest user agent
   are all the same statement in different words: *not this way*. The system's
   answer to every one of them is to stop, not to engineer around it.
3. **Be identifiable and reachable.** One honest user agent carrying a contact
   address, on every request, so an operator on the other end can ask us to stop
   without having to block an anonymous client first.
4. **Prefer the smaller, slower, legal path even when it costs coverage.** Two
   employers are missing from automated discovery because their boards are behind
   bot protection (`SOURCE_ADAPTERS.md` §11). That cost is bounded and known. The
   cost of the alternative is not.

---

## 2. What the system actually does, in one page

| Action | Does the system do it | Where |
|---|---|---|
| Fetch an employer's own public ATS JSON endpoint, unauthenticated | **Yes** | `SOURCE_ADAPTERS.md` §5–§6 |
| Read job-alert email the operator asked a platform to send them | **Yes** | `SOURCE_ADAPTERS.md` §7 |
| Read the operator's own mailbox for replies to their applications | **Yes**, read-only scope | `EMAIL_INGESTION.md` §2.3 |
| Send one email a day to the operator's own address | **Yes** | `EMAIL_INGESTION.md` §11 |
| Store a link to a deny-listed site as a human-clickable reference | **Yes** | `COMPANY_REGISTRY.md` §2.4 |
| Scrape LinkedIn, Indeed, Glassdoor, Naukri or any other deny-listed site | **No, ever** | §3 |
| Follow a link out of a job-alert email to enrich it | **No** | `SOURCE_ADAPTERS.md` §7.3 |
| Log into any employer or aggregator system | **No** | §5 — every adapter is unauthenticated by selection criterion |
| Run a headless browser to reach a board behind bot protection | **No** | `SOURCE_ADAPTERS.md` §11 |
| Impersonate a browser user agent, or rotate user agents | **No** | §7 |
| Submit an application | **No, at any version** | §9 |
| Email a recruiter, hiring manager or any third party | **No, at any version** | §10 |
| Retain the body of any email | **No** | §11 |

---

## 3. The never-scrape deny list

### 3.1 The list

A frozen code constant in `sources/policy.py`, checked on every request after
redirect resolution, with no setting, environment variable, admin toggle or
request parameter that disables it (`SECURITY_ARCHITECTURE.md` §5):

```python
NEVER_FETCH_HOSTS: frozenset[str] = frozenset({
    "linkedin.com", "www.linkedin.com", "in.linkedin.com",
    "naukri.com", "www.naukri.com",
    "indeed.com", "in.indeed.com", "www.indeed.com",
    "glassdoor.com", "www.glassdoor.co.in",
    "monsterindia.com", "shine.com", "instahyre.com",
    "angel.co", "wellfound.com",
    "facebook.com", "www.facebook.com",
})
```

`POST /api/v1/companies/detect` against any of these returns **403
`source.denied_by_policy`** before a request is issued, so the refusal is not
even observable to the denied host, and the message routes the operator to the
supported alternative rather than ending the conversation.

### 3.2 LinkedIn — first on the list, and the reason the list exists

LinkedIn is not one entry among many. It is the entry the list was written for,
and the reasoning is worth stating at length because every future contributor
will at some point think "but LinkedIn has all the jobs".

**(a) The terms prohibit it, explicitly and unambiguously.** LinkedIn's User
Agreement and its Professional Community Policies prohibit using bots or other
automated methods to access the service, scraping or copying profiles and data
through any means not expressly permitted, and using or developing third-party
software that scrapes. This is not an ambiguity to be litigated in a code review.
It is a plain prohibition on exactly the act being contemplated, in the contract
the operator personally accepted when they created their account.

**(b) The bot detection is aggressive, mature and adversarial.** LinkedIn
operates one of the most developed anti-automation programmes on the consumer
web: request fingerprinting, behavioural analysis, challenge interstitials, rate
anomaly detection, and account-level correlation that ties automated traffic back
to the logged-in identity that generated it. Defeating it is not a weekend
problem, and — decisively — *attempting* to defeat it is itself the violation.
There is no version of this where the system quietly succeeds.

**(c) The realistic outcome is account termination, not a warning.** The
enforcement action LinkedIn actually takes against detected scraping from an
authenticated session is restriction or permanent termination of the account.
Not a rate limit. Not an email. The account.

**(d) The trade is asymmetric to the point of absurdity.** This is the argument
that settles it. For a job seeker, LinkedIn is not a nice-to-have data source; it
is **the channel through which recruiters find them**. Inbound recruiter
messages, referral paths through former colleagues, the profile that a hiring
manager opens after reading a résumé — all of it lives there. So the wager is:

```
stake:   the primary channel through which opportunities arrive, permanently,
         plus the professional network built over a career
to win:  a somewhat richer job feed, for a system that already receives
         LinkedIn's own job alerts by email
```

No expected-value calculation makes that a good bet, at any probability of
detection. A tool built to help someone find a job must not be capable of
destroying the single most important asset they have for finding one. That is why
this is an invariant and not a setting: a setting is a thing someone turns on at
2 a.m. when a board is missing three roles.

**(e) The maintenance argument, which matters even setting ethics aside.** A
LinkedIn scraper is a permanent liability. It breaks on markup changes, it breaks
on flow changes, it breaks on challenge rollouts, and every break arrives as an
urgent failure in a daily pipeline. The email path has been stable for a decade
because an email template is a product surface with its own compatibility
pressure.

### 3.3 `hiQ Labs v. LinkedIn` — what it decided and what it did not

This case is invariably raised as though it settled the question. It did not, and
the distinction is precise enough to be worth stating correctly.

**What it addressed.** Whether scraping *publicly accessible* profile data —
pages viewable without logging in — constitutes access "without authorization"
under the US **Computer Fraud and Abuse Act**. The Ninth Circuit's reasoning, and
the Supreme Court's subsequent narrowing of the CFAA in *Van Buren*, pointed the
same way: the CFAA's anti-hacking provision is not the right tool against
scraping of data that is open to the public with no authentication gate.

**What it did not decide.**

1. **It did not make scraping contractually permitted.** The User Agreement is a
   separate obligation from the CFAA. The same litigation went on to address
   LinkedIn's **breach of contract** claim, and hiQ was ultimately found to have
   breached the User Agreement. The headline "scraping public data is legal" is a
   statement about one federal criminal statute, not about the contract every
   account holder accepted.
2. **It did not create a right of access.** LinkedIn remained free to block, to
   terminate accounts, and to make access technically harder. A finding that
   conduct is not a federal crime is not a finding that the counterparty must
   tolerate it.
3. **It says nothing about authenticated scraping.** Job listings of the kind
   this system would want are substantially behind a login. Once a session
   credential is used, the "publicly accessible" premise of the case is gone
   entirely.
4. **It is US law.** This system's operator is in India, and the applicable
   contract, the IT Act, and the Indian position on unauthorised access are not
   the Ninth Circuit's to determine.

**The conclusion drawn here:** *hiQ* is an argument about criminal liability for
one class of scraping under one statute. It is not permission, and treating it as
permission is how a project talks itself into a contract breach and a terminated
account. The design does not rely on it, does not need it, and is unaffected by
how any future case comes out — because the system does not scrape LinkedIn under
any legal theory.

### 3.4 Indeed

- **Terms.** Indeed's terms of service prohibit accessing the site by automated
  means, scraping, and using data mining or extraction tools without express
  written permission.
- **A licensed alternative exists and is the point.** Indeed operates a Publisher
  and Employer API programme with a contract, a key and quotas. That an official,
  contracted access route exists makes unlicensed scraping strictly worse: it is
  not a grey area where no channel is offered — it is declining the channel that
  is offered.
- **Bot protection.** Indeed employs commercial anti-automation. The same
  principle as §1.2(2) applies.
- **The sanctioned path is used instead.** Indeed job alerts arriving by email
  are parsed by `mail_alert` (`SOURCE_ADAPTERS.md` §7), with
  `alert@indeed.com` and `noreply@indeed.com` in the default sender list.
- **Risk if ignored:** IP blocking, legal notice, and the loss of the alert
  channel that currently works. There is no upside that the alert channel does
  not already provide.

### 3.5 Glassdoor

- **Terms.** Glassdoor's terms of use prohibit automated access, scraping and
  copying of content, and its content model depends on a
  contribute-to-view reciprocity that automated collection is designed to defeat.
- **The content is largely not what this system needs.** Glassdoor's value is
  reviews and salary data, not job postings — its listings are substantially
  syndicated from the same ATS boards this system already reads at the source, at
  higher fidelity, with a full description.
- **Third-party personal content.** Reviews are user-generated content written by
  identifiable-in-aggregate employees. Bulk-collecting them into a personal
  database is a personal-data decision, not just a terms decision, and there is no
  purpose in this system that needs it.
- **Risk if ignored:** blocking and legal exposure, for data the system has no
  designed use for. This is the easiest entry on the list to justify.

### 3.6 The rest of the list

| Host | Why it is listed |
|---|---|
| `naukri.com` | India's largest job aggregator. Terms prohibit automated access; the listings are recruiter-posted and duplicated; the sanctioned path is the Naukri alert email (`info@naukri.com`), already parsed |
| `monsterindia.com`, `shine.com`, `instahyre.com` | Aggregators with equivalent terms and equivalent anti-automation posture; equivalent duplication of ATS-sourced listings; no API offered on terms this project can meet |
| `angel.co`, `wellfound.com` | Same operator, same posture. Startup listings largely reappear on the startups' own Greenhouse, Lever or Ashby boards, which is where this system reads them |
| `facebook.com` | Present because of Meta's careers board (`SOURCE_ADAPTERS.md` §11). Reaching it requires executing page JavaScript to obtain a rotating persisted-query hash, a CSRF token and a session token — a signed-out session impersonation, which §1.2(2) forbids |

Two entries deserve a note. `wellfound.com` and `angel.co` are listed even though
their listings are the *least* protected of the group, because the deny list's
value comes from being a bright line rather than a per-site judgement call. And
`facebook.com` is listed even though the target is a careers board rather than the
social product, because the access technique — not the content — is what is
being refused.

### 3.7 Adding to and removing from the list

- **Adding** is a one-line change with a paragraph of reasoning in §3.6 and a
  released deploy. Nothing more.
- **Removing** requires all of: the operator of the site publishing a documented
  public API or an unauthenticated JSON endpoint that its own front end consumes,
  terms that do not prohibit the access, a robots.txt that does not disallow it,
  and no bot-protection layer to defeat. That is the same bar as §5's selection
  criterion, and a site meeting it would be added as a normal adapter rather than
  argued off a list.
- **A missing role is never a reason.** The two supported answers are always: set
  up a job alert into the alerts mailbox, or paste the role into
  `POST /api/v1/postings/import`.

---

## 4. The sanctioned-channel principle

### 4.1 Two channels, both invitations

**Channel one — the employer's own ATS.** When Stripe's careers page loads, the
browser fetches `boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true`. No
key, no cookie, no session, no token. That endpoint exists so the world can read
Stripe's open roles, and it is served by Greenhouse to anyone who asks. Every
adapter in this system reads an endpoint of exactly that character, and the
selection criterion is explicit: *an adapter that would need a scraped session
cookie, a reverse-engineered token or a headless browser to log in does not get
written* (`SOURCE_ADAPTERS.md` §3).

**Channel two — job-alert email the platform chose to send.** The operator, as a
LinkedIn (or Naukri, or Indeed) user, configures job alerts in their own account
and points them at a dedicated mailbox. The platform then sends alert emails to
that mailbox, as a normal product feature, at the operator's own request. The
`mail_alert` adapter reads *that mailbox*, over the Gmail API, with the
operator's own OAuth grant.

### 4.2 Receiving a LinkedIn alert is categorically different from scraping LinkedIn

This is the distinction most likely to be misread by a future reader, so it is
stated as a table and then defended.

| | Scraping LinkedIn | Parsing a LinkedIn job alert |
|---|---|---|
| What is accessed | LinkedIn's servers | The operator's own Gmail mailbox |
| Whose credential is used | A LinkedIn session cookie belonging to a person, replayed by a machine | A Gmail OAuth token for the operator's own mailbox. **No LinkedIn credential exists in the system** |
| Who chose the volume | The scraper | LinkedIn, via its own alert cadence |
| Load imposed on LinkedIn | Real, and unwanted | **Zero.** LinkedIn already sent the mail |
| Controls defeated | Rate limits, bot detection, possibly a login wall | None. There is nothing to defeat |
| Contract position | Prohibited by the User Agreement | The User Agreement does not, and could not, prohibit a user from reading email LinkedIn deliberately sent them |
| Failure mode | Account restriction or termination | The parser stops matching and logs a `parse_misses` warning |
| Durability | Breaks on every markup change | Stable for a decade; an email template is a product surface |

**Reading your own inbox is not accessing LinkedIn's service.** That sentence is
the whole principle. The email was addressed to the operator, delivered to a
mailbox they control, at a cadence LinkedIn chose, containing content LinkedIn
composed for them to read. A machine reading it on their behalf is the operator
reading their own mail with assistance — the same category as an email client, a
filter rule, or a person who reads faster.

It is not a loophole, and the test for that is simple: **a loophole is an
argument that stops working when the counterparty notices.** If LinkedIn learned
that a user's job alerts were being parsed by that user's own tooling, nothing
about it would trouble them — no term is breached, no load is imposed, no control
is bypassed, and the alert served its purpose, which was to get the user to look
at a job.

### 4.3 The cost of choosing this channel, accepted

An alert email carries a title, a company, a location and a link. It does not
carry a job description. Consequences, all deliberate:

- `mail_alert` has the lowest fidelity rank in the system (**20** against 72–95
  for everything else), so the moment the same role appears on the employer's own
  board, the full-fidelity record supersedes the stub
  (`SOURCE_ADAPTERS.md` §8).
- Mail-sourced postings are flagged `needs_description` and **skip requirement
  extraction entirely** — running the extractor on a five-line stub would produce
  confident, worthless requirements and then score against them.
- They surface as unscored discovery leads. The operator's action is to open the
  link by hand or, far better, to add that company to the registry so its real
  board is polled.
- **The alert link is stored and never followed.** Not to enrich the stub, not
  once. It is rendered as a link for a human to click.

This is a real loss of quality, and it is the price of the principle. It is paid
rather than argued away.

---

## 5. Per-source legal basis

### 5.1 The selection criterion

An adapter is written only for an endpoint that is:

1. reachable **without authentication**,
2. reachable **without a browser session** or an executed-JavaScript token,
3. reachable **without defeating bot protection**,
4. on a host **absent from `NEVER_FETCH_HOSTS`**,
5. **not disallowed** by the host's robots.txt for our user agent, and
6. served by the employer or their ATS vendor **as the backing API of a public
   careers page**, or documented as a public API.

All six, every time. Failing any one of them means the answer is `mail_alert` or
manual import, not a cleverer adapter (`SOURCE_ADAPTERS.md` §12, step 1).

### 5.2 The table

| Source | Access method | Terms position | Auth | Rate limit applied | robots.txt | Poll interval | Risk |
|---|---|---|---|---|---|---|---|
| **Greenhouse** | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | Documented public job board API; the endpoint backs employers' own careers pages | None | 5 req/s, burst 10, shared bucket | Checked; fail-open (documented JSON API) | 1440 min | **Low** |
| **Lever** | `GET api.lever.co/v0/postings/{site}?mode=json` | Documented public postings API | None | 5 req/s, burst 10, shared | Checked; fail-open | 1440 min | **Low** |
| **Ashby** | `GET api.ashbyhq.com/posting-api/job-board/{board}` | Documented public job-board API. The authenticated `/api/*` endpoints exist and are **not** used | None | 4 req/s, burst 8, shared | Checked; fail-open | 1440 min | **Low** |
| **Workday (CXS)** | `POST {tenant}.wdN.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` + per-posting detail | Undocumented but public: these are the endpoints Workday's own public careers front end calls, unauthenticated, per employer tenant | None | **1 req/s, burst 3, per tenant host** | Checked per host; **fail-closed** if unreachable | 1440 min | **Low–moderate** — undocumented shape, and the heaviest adapter, hence the tightest bucket |
| **SmartRecruiters** | `GET api.smartrecruiters.com/v1/companies/{id}/postings` + detail | Documented public postings API | None | 3 req/s, burst 6, shared | Checked; fail-open | 1440 min | **Low** |
| **Workable** | `POST apply.workable.com/api/v3/accounts/{account}/jobs` + detail | Public board API backing `apply.workable.com` | None | 3 req/s, burst 6, shared | Checked; fail-open | 1440 min | **Low** |
| **Recruitee** | `GET {company}.recruitee.com/api/offers/` | Documented public offers API | None | 2 req/s, burst 4, per tenant | Checked per host; fail-closed | 1440 min | **Low** |
| **Google Careers** | Public careers search endpoint, keyword + country narrowed | Public careers site backing API; shape has drifted historically | None | 1 req/s, burst 2 | Checked; fail-closed | 720 min | **Moderate** — shape drift, single employer |
| **Amazon Jobs** | `www.amazon.jobs` public search JSON | Public careers site backing API | None | 1 req/s, burst 2 | Checked; fail-closed | 720 min | **Moderate** |
| **Microsoft Careers** | `gcsservices.careers.microsoft.com` public search + detail | Public careers site backing API | None | 1 req/s, burst 2 | Checked; fail-open (documented JSON host) | 720 min | **Moderate** |
| **Job-alert email** | Gmail API `users.messages` over the operator's own OAuth grant | Reading the operator's own mailbox. No platform terms engaged | Gmail OAuth (`gmail.readonly`) | Gmail API quota units; hourly poll | n/a | 60 min | **Low** |
| **Reply mail** | Gmail API, same grant | Same | Gmail OAuth (`gmail.readonly`) | Gmail quota | n/a | 60 min | **Low** |
| **Digest send** | Gmail API `users.messages.send`, single hardcoded recipient | Sending to the operator's own address | Gmail OAuth (`gmail.send`) | 1 message/day | n/a | Daily 08:15 IST | **Low** |
| **Manual import** | Operator pastes a URL or a description | The operator's own act | None | n/a | Policy gate still applies to the URL | On demand | **Low** |
| **Company detection** | One `GET` of a pasted careers URL, ≤ 3 redirects, 512 KB cap, no JS | A single request to a public page, of the kind a browser makes | None | Same buckets as a real run | Honoured | On demand | **Moderate** — the only free-destination fetch; see `SECURITY_ARCHITECTURE.md` §4 |
| **Meta careers** | — | **Deferred.** Requires executing page JavaScript for a rotating persisted-query hash and CSRF/session tokens | — | — | — | — | **Excluded** |
| **Apple careers** | — | **Deferred.** JSON endpoint behind a page-issued CSRF token and a fingerprinting bot-protection layer | — | — | — | — | **Excluded** |

### 5.3 Reading the risk column

- **Low** — a public, unauthenticated endpoint the vendor serves as a product
  surface, polled once a day at a conservative rate, with an identifying user
  agent. The realistic worst case is a 403, which is treated as a refusal and
  ends the adapter's life for that source.
- **Moderate** — the same, but on an undocumented shape (so it can change under
  us and must fail loudly rather than silently), or a free-destination fetch, or
  a single-employer endpoint where our traffic is not lost among many tenants.
- **Excluded** — access would require defeating a control. Not attempted, and the
  enum value does not exist so that nobody is tempted (`SOURCE_ADAPTERS.md` §11).

### 5.4 Recording the finding

`SOURCE_ADAPTERS.md` §12 step 1 requires that the permissibility finding for any
new source is recorded **here, whether the answer is yes or no**. A rejected
source with a written reason is more valuable than silence, because silence gets
re-litigated every six months.

---

## 6. Robots.txt policy

Invariant 8: every adapter declares its polling interval and honours `robots.txt`
and any documented rate limit.

| Rule | Behaviour |
|---|---|
| Fetch | Once per host per run, cached in Redis 24 h under `robots:{scheme}://{host}`, parsed with `urllib.robotparser` |
| Evaluation | Against our real user agent **and** against `*`; the more restrictive wins |
| `Disallow` covering our endpoint | The source is **disabled** and reported in the digest. Never bypassed, and there is no override |
| `Crawl-delay` | **Lowers** that host's token-bucket rate for the run. It never raises it |
| Unreachable robots.txt — documented JSON API hosts | **Fail open**, for `boards-api.greenhouse.io`, `api.lever.co`, `api.ashbyhq.com`, `api.smartrecruiters.com`, `apply.workable.com`, `gcsservices.careers.microsoft.com` |
| Unreachable robots.txt — every other host | **Fail closed** for that run |

The asymmetry in the last two rows is a deliberate decision and the reasoning is
worth keeping: a documented public JSON API is an invitation, and the absence of
a robots file on an API host is not a refusal. An employer careers host that will
not serve robots.txt has told us nothing, and the conservative reading wins.

A `robots_denied` outcome disables the source immediately rather than counting
toward the consecutive-failure threshold, because it is a policy answer, not a
transient one.

---

## 7. User-agent policy

One identifying user agent for all outbound traffic, from
`settings.source_user_agent`:

```
ScoutCareers/1.0 (personal job-search agent; +mailto:<operator address>)
```

| Decision | Reasoning |
|---|---|
| **It is honest.** Never a browser string | Impersonating a browser to evade bot detection is the same act as scraping a source that does not want to be scraped |
| **It carries a contact address** | An operator on the other end can ask us to stop, and we can comply, without having to block an anonymous client first |
| **It never varies per source, and never rotates** | A rotating user agent is an evasion technique, and there is nothing here to evade |
| **The address is configuration, not a code constant** | So it is not committed to the repository |
| **A 403 to this UA is a policy signal** | The adapter is retired or the source disabled. It is never a prompt to change the user agent |

That last row is the rule that stops the "just spoof Chrome" pull request, and it
is the single most load-bearing line in this section.

---

## 8. Rate limits

### 8.1 Per-host buckets

Per-source token buckets in Redis, keyed on the **rate-limiting domain** rather
than the source ID, so several Adobe Workday sites sharing one tenant host share
one bucket (`SOURCE_ADAPTERS.md` §4.3):

| Adapter | Bucket key | Rate | Burst |
|---|---|---|---|
| greenhouse | `boards-api.greenhouse.io` | 5 req/s | 10 |
| lever | `api.lever.co` | 5 req/s | 10 |
| ashby | `api.ashbyhq.com` | 4 req/s | 8 |
| smartrecruiters | `api.smartrecruiters.com` | 3 req/s | 6 |
| workable | `apply.workable.com` | 3 req/s | 6 |
| recruitee | `{company}.recruitee.com` | 2 req/s | 4 |
| workday | `{host}` (per tenant) | **1 req/s** | 3 |
| google | `careers.google.com` | 1 req/s | 2 |
| amazon | `www.amazon.jobs` | 1 req/s | 2 |
| microsoft | `gcsservices.careers.microsoft.com` | 1 req/s | 2 |
| mail_alert | `gmail` | Gmail API quota units | — |

**These are conservative floors chosen without reference to any published quota,
because most of these APIs publish none.** Where a vendor documents a limit, the
documented limit is recorded here and the bucket is set to the **lower of the
two**. The system never probes for a limit by exceeding it.

Retries re-acquire a token like any other request. Backing off and then bursting
is how a well-behaved client becomes a badly-behaved one.

### 8.2 Polling intervals

| Source class | Interval | Why |
|---|---|---|
| Every multi-tenant ATS source | **1440 min** (daily) | A job posting does not change hourly. Daily is enough to see a role on the day it appears, and it is the politest cadence that still meets the product goal |
| google, amazon, microsoft | **720 min** (twice daily) | Very large boards with high turnover; still only two passes |
| mail_alert | **60 min** | Reads a mailbox, not an employer. Imposes no third-party load |
| Discovery run trigger | **08:00 IST**, once | `ARCHITECTURE.md` §6 |
| Digest | **08:15 IST**, once | One message, one recipient |

A source with five consecutive failures is auto-disabled and reported, never
silently retried forever. `rate_limited` and `circuit_open` outcomes do **not**
count toward that threshold, because they are our own back-pressure and not the
source's fault.

### 8.3 The daily request budget

Order-of-magnitude, for 300 companies and roughly 320 sources:

| Destination | Sources | Requests per run | Daily total | Notes |
|---|---|---|---|---|
| `boards-api.greenhouse.io` | ~80 | 1 each | ~80 | Whole board in one response |
| `api.lever.co` | ~25 | 1 each | ~25 | Whole board in one response |
| `api.ashbyhq.com` | ~25 | 1 each | ~25 | Whole board in one response |
| `*.myworkdayjobs.com` | ~60 tenants | list pages + detail fan-out, bounded by the 180 s per-source ceiling at 1 req/s | **≤ 180 per tenant host** | Different host per employer; no single host sees more than this |
| `api.smartrecruiters.com` | ~20 | ~101 each (list + per-posting detail) | ~2,000 | The most expensive shared host; SmartRecruiters companies are worth tiering carefully |
| `apply.workable.com` | ~30 | ~1 + one per posting on small boards | ~1,400 | Small boards, cheap details |
| `*.recruitee.com` | ~10 | ~2 each | ~20 | |
| `careers.google.com`, `www.amazon.jobs`, `gcsservices.careers.microsoft.com` | 3 | per configured query | low hundreds each | Two runs a day |
| Gmail API | 1 | metadata + message fetches | Well inside the free quota | 24 polls a day |

The two structural facts worth naming: **Workday load is spread across ~60
distinct tenant hosts**, so no single employer sees more than a few minutes of
1 req/s traffic once a day; and **SmartRecruiters is the one shared host that
sees four-figure daily traffic**, which is exactly why its bucket is 3 req/s and
why the per-source detail cost is called out in `SOURCE_ADAPTERS.md` §5.5.

### 8.4 Behaviour when a source pushes back

| Signal | Response |
|---|---|
| `429` with `Retry-After` | Honour it, capped at 30 s. Past the cap the source fails for this run rather than blocking the run budget |
| `429` without `Retry-After` | Exponential backoff with full jitter, max 4 attempts, per-source retry budget 90 s |
| `403` to our honest UA | **A refusal.** Recorded, source disabled. Not retried with different headers, ever |
| `robots.txt` `Disallow` | Source disabled, reported in the digest |
| Repeated 5xx | In-run circuit breaker opens for that bucket; cross-run counter auto-disables at 5 |
| A request from the source operator to stop | §13 |

---

## 9. Why automated submission is excluded

### 9.1 The invariant, and then the reasoning

Invariant 1 says the system never submits. The reasoning matters more than the
rule, because a rule without reasoning gets relaxed by whoever inherits it.

### 9.2 The shared-ATS argument

This is the decisive one, and it is not obvious until it is stated.

Workday, Greenhouse, Lever and SmartRecruiters are not four employers. They are
four platforms that between them serve **tens of thousands of employers**, and an
applicant's identity — email address, phone number, name, résumé fingerprint —
is visible to the platform across all of them. A candidate profile on a shared
ATS is not scoped to one company's hiring team.

So consider what volume auto-submission actually produces:

```
200 applications/week through one applicant identity
  → the same résumé, minimally varied, submitted at machine cadence
  → across dozens of employers on the same platform
  → detected as bulk-application behaviour by the platform, not by any one employer
  → the applicant is flagged
  → and the flag follows them to the employers they actually care about
```

That last line is the whole argument. The cost of being marked a spam applicant
is not paid at the hundred volume-tier companies the mass submission targeted. It
is paid at the **dream-tier** company that runs on the same platform and whose
recruiter sees a flagged profile or a de-prioritised application six months
later. The applicant will never know that is what happened.

There is no undo. Unlike a rate limit or an IP block, an applicant-reputation
signal on a shared ATS is not something a code change can clear. `A9` in
`SECURITY_ARCHITECTURE.md` §2.1 exists as an asset entry for exactly this.

### 9.3 The conversion arithmetic

Auto-submission is not only risky. It is *worse at the job*, and the arithmetic
says so.

Widely reported ranges for application-to-response rates:

| Approach | Response rate |
|---|---|
| Generic résumé, high volume, untailored | **1–2%** |
| Tailored résumé and letter, targeted at a role the applicant genuinely matches | **10–15%** |

Take the conservative end of both — 1.5% and 12.5% — and ask what it costs to
reach **20 responses**:

```
generic:   20 ÷ 0.015 = 1,333 applications
tailored:  20 ÷ 0.125 =   160 applications

ratio: 8.3× fewer applications for the same number of responses
```

Now cost the human time. Scout Careers is designed for **ten minutes a day**,
producing **five to ten well-targeted applications per week**
(`ARCHITECTURE.md` §1.1):

```
tailored:  160 applications ÷ 8 per week ≈ 20 weeks, at ~70 minutes of human time
           per week — and every one is a document the operator read before sending

generic:   1,333 submissions in the same 20 weeks ≈ 67 per week through one
           identity on a handful of shared platforms — which is precisely the
           volume signature §9.2 describes
```

And the responses are not equivalent. A response to a tailored application is
disproportionately a real conversation; a response to a generic blast is
disproportionately an automated acknowledgement or a fast rejection, because the
applicant did not match the role in the first place.

The conclusion: **the practice that carries the reputational risk is also the one
that performs worse.** There is no trade-off being made here. Auto-submission is
excluded because it is bad, and its being non-compliant is the second reason, not
the first.

### 9.4 The terms position

Separately from all of the above, the terms of use of the major ATS platforms
generally prohibit automated or scripted submission of applications and the use
of bots against their candidate-facing flows, and many application forms carry a
CAPTCHA whose sole purpose is to state that a human is required. Completing a
CAPTCHA is enumerated in invariant 1 as something the system does not do, for the
same reason the user-agent policy forbids browser impersonation: it is defeating
a control that exists to say *not this way*.

### 9.5 How the exclusion is enforced

Not by policy, by absence:

- **There is no submission endpoint at any version** (`API.md` §8). The most
  reliable way to enforce a rule is to give it nowhere to be called from.
- `POST /review/{id}/approve` is named deliberately: **approve means "I am going
  to submit this myself."** It creates the `application` row, freezes the
  artifacts, and returns download links. It does not contact the employer.
- Playwright is in the stack only for the cases where no API exists on the
  *discovery* side, and no browser-automation adapter for a hostile endpoint is
  written at all (`SOURCE_ADAPTERS.md` §11).
- The pipeline's two `[HUMAN]` steps (`ARCHITECTURE.md` §6.1) are not
  configurable.

### 9.6 What the system does instead

It automates the half that is genuinely high-volume, low-signal and repetitive —
discovery, deduplication, filtering, requirement extraction, coverage scoring,
ranking, drafting — and hands a human a ranked queue with the gaps stated
honestly. Ten minutes of review, then the human submits. That is not a compromise
forced by compliance. It is the design that works better.

---

## 10. Why automated outbound email is excluded

### 10.1 The invariant

Invariant 2: the system sends exactly one class of email — the daily digest, to
the operator's own address. It never emails a recruiter, hiring manager or any
third party.

The tempting feature is obvious: the ATS payloads sometimes contain a recruiter
name; the reply mail contains real addresses; a "follow up automatically after 7
days" button would take an afternoon to build. It is excluded, and the reasons
are cumulative rather than alternative.

### 10.2 It is spam, by the recipient's definition

Unsolicited, automated, bulk, commercial-in-purpose contact using an address
obtained from a third-party source is spam. Not "arguably". The recipient did not
ask, does not know the sender, and receives the message because a program decided
to send it. The sender's belief that the message is personalised and relevant is
not the test — the recipient's experience is, and recruiters receive this volume
daily.

### 10.3 Deliverability collapse and sender-reputation damage

This is the mechanical consequence, and it is worse than it first appears because
it is not confined to the campaign.

```
automated cold sends from the operator's own Gmail identity
  → low engagement, spam reports, hard bounces on stale scraped addresses
  → provider reputation for that sending identity and domain degrades
  → the operator's ordinary mail starts landing in spam:
      the reply to a recruiter who did write back,
      the thank-you note after an interview,
      the acceptance of an offer
```

A degraded sender reputation is slow to build and slow to repair, and it damages
exactly the correspondence the whole system exists to support. Sending automated
mail from the identity you need employers to trust is self-defeating in the most
literal way. Google's bulk-sender requirements — authentication, low complaint
rates, one-click unsubscribe — additionally make automated outbound from a
personal Gmail identity a policy problem as well as a reputation one.

### 10.4 DPDP Act 2023 exposure

India's **Digital Personal Data Protection Act, 2023** governs the processing of
digital personal data. A recruiter's name, email address and employer are
personal data, and using them to send automated outbound contact is processing
for a purpose the person never consented to.

The Act's exemption for processing by an individual **for any personal or
domestic purpose** is what makes the rest of this system comfortable (§11.4).
Automated outbound contact at scale, using addresses gathered from third-party
sources, is precisely the activity most likely to be argued out of that
exemption — it is outward-facing, it is systematic, it involves people who have
no relationship with the operator, and it is not plausibly "domestic".

The design consequence is simple: the system stays firmly inside the exemption by
never contacting anyone. It does not attempt to reason about where the line is,
because the line is not worth locating when standing well behind it costs
nothing.

### 10.5 GDPR and ePrivacy exposure

If any recipient is in the EU or the UK — entirely plausible for an operator
applying to multinational employers — the analysis is stricter still:

- **The household exemption does not cover it.** GDPR Article 2(2)(c) excludes
  processing "in the course of a purely personal or household activity", and
  systematic automated outbound contact using compiled third-party addresses is
  not that.
- A lawful basis would be required. "Legitimate interests" for unsolicited
  automated contact is a weak argument that has been rejected in comparable
  contexts, and consent plainly does not exist.
- **Transparency obligations bite.** Article 14 requires notifying a person whose
  data was obtained from a source other than themselves — a requirement that
  cold-outreach tooling never satisfies.
- The ePrivacy Directive as implemented nationally (PECR in the UK) adds direct
  rules on unsolicited electronic marketing.
- Data-subject rights — access, erasure, objection — would attach to a personal
  database of recruiter contacts held for outbound purposes.

The same paragraph in different words applies to US recipients under CAN-SPAM
(sender identification, functioning opt-out, honest headers) and to unsolicited
commercial communication rules in India. None of this is worth engineering around
for a feature the next section shows does not work anyway.

### 10.6 The practical point

**One warm referral outperforms a thousand cold sends.**

A cold email to a recruiter competes with the fifty other cold emails they
received that morning and carries no signal about the sender other than
willingness to send cold email. A referral — a former colleague forwarding a
résumé internally, a hiring manager who was asked about the role by someone they
trust — arrives pre-vouched, is read, and converts at a rate that no volume of
outbound approaches.

The data model reflects this rather than merely asserting it:
`application.source_channel` distinguishes `direct` from `referral` and
`referral_contact` records who, and `GET /api/v1/metrics/funnel` can group by
`source_channel`. After forty applications the operator will have measured their
own referral-versus-direct conversion from their own history, which is the only
honest number the system will ever produce on the subject.

So the effort that automated outbound would have consumed is redirected into the
one activity that works, and it is redirected by a human, because that is the
only way it works at all.

### 10.7 How the exclusion is enforced

- `gmail.send` is the narrowest send capability Google offers, and **there is no
  scope for "send only to yourself"**. The OAuth grant is therefore broader than
  the system's behaviour, and the gap is closed by an assertion and a test in
  this codebase, not by the platform. This is stated plainly rather than glossed
  (`EMAIL_INGESTION.md` §2.3, `SECURITY_ARCHITECTURE.md` §14.10).
- The recipient of every outbound message is the configured operator address.
  A test asserts that no code path constructs a message with any other recipient.
- **There is no endpoint for sending mail to an arbitrary address**, at any
  version (`API.md` §8).
- The mail module never replies to an alert, never replies to a recruiter, and
  never follows a URL found in a body.
- `gmail.modify` is deliberately not requested, so the system cannot even alter
  the mailbox it reads (`EMAIL_INGESTION.md` §2.4).

---

## 11. Personal data handling

### 11.1 What personal data the system holds

| Data subject | Category | Source | Purpose | Retention |
|---|---|---|---|---|
| **The operator** | Résumé content: employment history, education, skills, project descriptions | Their own files | Matching and document generation | Kept until deleted |
| The operator | The claims ledger, including internal cost and volume figures from prior work | Their own verification | Preventing fabrication in generated documents | Soft-deleted only; provenance never orphaned |
| The operator | Application history: employer, role, dates, status, outcomes | The system's own records | Funnel metrics | Kept until deleted |
| The operator | Gmail OAuth token, mailbox metadata | Google, with consent | Alert parsing and reply classification | Until revoked |
| **Recruiters and employer staff** | `from_address`, `from_domain`, `subject`, `received_at` on received mail | Mail they sent to the operator | Linking a reply to an application; justifying a status change | **24 months**, then hard-deleted |
| Recruiters and employer staff | A ≤ 200-character verbatim excerpt on the event row | The message they sent | Making a status change defensible | With the event |
| Third parties named in ATS metadata (job owners, requisition creators) | Names, emails, phone numbers | ATS payloads | **None** | **Not stored** — dropped at the adapter boundary |
| Referral contacts | A free-text name the operator types | The operator | Attribution on `application.source_channel` | With the application |

### 11.2 The minimisation choices already made in the data model

These are not policy statements. They are properties of the schema, which is why
they hold.

1. **`email_message` has no body column.** Bodies are classified in memory and
   dropped when the function returns. There is no cache, no `raw` JSONB, no MIME
   spool, no debug dump. The guarantee cannot be violated by a code path because
   there is nowhere for the data to go (`DATA_MODEL.md` §8.2,
   `EMAIL_INGESTION.md` §9.1).
2. **The excerpt is capped at 200 characters and must be a verbatim substring**
   of the message. Enough to justify "why does this say rejected?", not enough to
   reconstitute the correspondence.
3. **Recruiter contact details are excluded from `RawPosting.raw` categorically**
   — SmartRecruiters `creator`, Lever owner fields and their equivalents. The
   system has no use for them, and invariant 2 means it will never contact them
   (`SOURCE_ADAPTERS.md` §4.6).
4. **`from_domain` is logged; `from_address` is not.** Subjects and excerpts are
   never logged.
5. **Alert links are stored, never followed**, so no tracking pixel is fired and
   no third-party profile of the operator's reading behaviour is created.
6. **Remote images are never loaded** by the mail parsers, for the same reason.
7. **`subject` storage is togglable** (`MAIL_STORE_SUBJECT=false` truncates it to
   40 characters), because it is the one stored field a cautious operator might
   want reduced.

The general principle: **the cheapest way to protect data is not to hold it**,
and the second cheapest is to hold a pointer to where its real custodian holds
it. Gmail retains the mail; this system stores `gmail_id` and `thread_id` so the
operator can open the original in one click.

### 11.3 Third-party data the operator did not ask for

Recruiter mail is written by real people who wrote to a **person**, not to a
system. Retaining their words in an application database is retention the
operator was not asked for and cannot justify on anyone's behalf. That is the
reasoning behind §11.2(1) and (2), and it is an ethical position before it is a
legal one.

### 11.4 The DPDP Act 2023 position

The **Digital Personal Data Protection Act, 2023** provides an exemption for
personal data processed by an individual for any **personal or domestic
purpose**. A single person running a tool on their own machine to manage their
own job search, holding their own résumé data and a small volume of metadata
about mail sent to them, sits squarely inside that exemption as drafted.

Three notes on how that exemption is treated here:

1. **It is not leaned on.** The system's data handling would be defensible
   without it: minimal collection, a stated purpose, bounded retention, no
   sharing, no profiling of third parties, no outbound contact. The exemption is
   a reason not to build a consent-management system, not a licence to collect
   more.
2. **The behaviours that would jeopardise it are the excluded ones.** Automated
   outbound contact (§10) and bulk collection of third-party personal data (the
   Glassdoor reasoning in §3.5) are the two activities that would make "personal
   or domestic purpose" a strained description. Both are excluded on independent
   grounds, and the exemption's durability is a further reason.
3. **It is single-user-shaped.** The moment the tool served a second person, or
   was offered to others, the exemption would not apply and a data-fiduciary
   analysis would be required. That is one of the reasons multi-user is treated
   as a different product rather than a feature
   (`SECURITY_ARCHITECTURE.md` §6.6).

For the résumé data itself the operator is the data principal and the data
fiduciary simultaneously, which is a legally uninteresting situation and a
practically important one: the risk is a breach, not a compliance failure, and
the controls that matter are in `SECURITY_ARCHITECTURE.md` §8.

### 11.5 Retention

| Data | Retention | Mechanism |
|---|---|---|
| `job_posting` for closed roles | 180 days after `closed_at` | Scheduled hard delete, cascading |
| `email_message` metadata (including recruiter addresses) | **24 months** | Scheduled hard delete |
| `application_event.excerpt` | With the event | Survives the message row as the justification for a status change |
| `application`, `application_event` | Until the operator deletes them | The funnel history the tracking design exists to produce |
| `claim` | Soft delete only | `claim_usage` provenance must never be orphaned |
| Artifacts for `skipped` review items | 90 days | File and row removed |
| `run_log` | 180 days | Hard delete |
| Structured logs | 30 days | Rotation |

Retention runs as a scheduled job whose deletions are recorded in `run_log`.
Retention by intention is not retention.

---

## 12. Compliance checklist

Re-verified before each release. Every item is a yes/no with evidence, not a
judgement call.

| # | Check | Evidence |
|---|---|---|
| C1 | `NEVER_FETCH_HOSTS` is unchanged, or the change is documented in §3.6 with reasoning | Diff review |
| C2 | The deny list is still unreachable from `Settings`; no environment binding exists | `test_denylist_not_configurable` |
| C3 | Every adapter's constructible URLs pass `assert_fetch_allowed` | `test_no_disallowed_host`, per adapter |
| C4 | No new source was added without a §5.2 row and a permissibility finding | §5.4 |
| C5 | No adapter authenticates, carries a cookie, or executes JavaScript | Code review + `trust_env=False` assertion |
| C6 | The user agent is still the honest identifying string, not per-source and not rotating | `test_user_agent_constant` |
| C7 | No `403` handler retries with different headers or a different UA | Code review |
| C8 | robots.txt handling is unchanged: `Disallow` disables, `Crawl-delay` only lowers | `test_robots_*` |
| C9 | No rate-limit bucket was raised without a documented vendor limit that is higher | §8.1 diff |
| C10 | No polling interval was shortened without a stated reason | §8.2 diff |
| C11 | **No endpoint exists that submits an application** | `test_no_submit_route` — enumerates the OpenAPI schema |
| C12 | **No code path constructs an outbound message to any address but the operator's** | `test_single_recipient` |
| C13 | Gmail scopes are still exactly `gmail.readonly` and `gmail.send` | `test_oauth_scopes` |
| C14 | No new column stores an email body, or a recruiter's name or phone number | Migration review against §11.1 |
| C15 | Any new personal-data field has a retention rule in §11.5 | Review |
| C16 | Excerpts are still capped at 200 characters and verbatim-checked | `test_excerpt_verbatim` |
| C17 | No new URL is fetched from a body, an alert or a job description | Code review |
| C18 | `pip-audit` and `npm audit` pass in `ci/run-checks.sh` | CI output |
| C19 | The adversarial slice of the eval golden set still scores 1.00 | Eval report |
| C20 | This document and `ARCHITECTURE.md` §3 still agree | Read both |

C11 and C12 are the two that must never be waived. If either fails, the release
does not ship — not with a flag, not behind a setting, not "temporarily".

---

## 13. If a source operator objects

### 13.1 Recognising an objection

| Signal | Interpretation |
|---|---|
| `403` to our honest, identified user agent | **A refusal.** Not a transient failure |
| A `robots.txt` change that disallows our endpoint | A refusal, expressed the standard way |
| A `429` that persists after backoff | A request to slow down, or to stop |
| Email to the address in our user agent | An explicit objection, and the reason the address is there |
| A legal notice, a cease-and-desist, an abuse report to the host | An explicit objection with escalation |
| A vendor publishing terms that newly prohibit this access | A prospective refusal |

### 13.2 Response procedure

1. **Stop immediately.** Disable the source (`PATCH /api/v1/sources/{id}` with
   `enabled: false`) — or for a vendor-wide objection, every source on that
   adapter — before anything else, including before replying. Stopping first is
   not an admission; it is the only response that costs the objector nothing
   further.
2. **Confirm the traffic actually stopped.** Check the next `run_log` for zero
   requests to that bucket key.
3. **Reply, if a human contacted us**, within 48 hours: who we are (one person,
   one personal job search), what was fetched (public job listings from a
   specific endpoint), at what rate, that it has already stopped, and that it
   will not resume without their agreement. No arguing, no *hiQ* citation, no
   explanation of why we were entitled to.
4. **Record it here** — a dated row in §5.2 with the risk changed to
   **Excluded** and a one-line reason. The record is the point: a source removed
   without a written reason gets re-added by whoever comes next.
5. **Add the host to `NEVER_FETCH_HOSTS`** if the objection is categorical rather
   than about rate, so the refusal survives any future configuration.
6. **Route the operator to the alternative**: a job alert into the alerts mailbox
   for that platform, or `POST /api/v1/postings/import` for a specific role.
7. **Do not resume** unless the source operator explicitly agrees, in writing, on
   terms recorded here.

### 13.3 The standing posture

The user agent carries a contact address specifically so that step 3 is possible
without the objector having to block an anonymous client first. A system that
makes itself easy to stop is a system that rarely has to be stopped, and this
posture is worth more than any amount of throughput.

---

## 14. The standing rejection

Any future feature request that touches:

- **submitting an application** to an employer, by HTTP, by browser automation,
  or by any other means,
- **outbound contact** with a recruiter, a hiring manager or any third party, by
  email or otherwise, or
- **fetching a host on the never-scrape list**, under any configuration, through
  any proxy, at any rate, "just to test",

is **rejected at design time**. It is not evaluated case by case, it is not
prototyped behind a flag, and it does not get a "we could do it carefully"
discussion. The reasoning is in §9, §10 and §3, it has already been had, and it
does not improve on re-litigation.

The invariants derive their entire value from being absolute. An invariant with
one carefully-argued exception is a default, a default is a setting, and a
setting is what someone changes at 2 a.m. when a board is missing three roles.

The answer to "but this one case is different" is: set up a job alert, or paste
the role into `POST /api/v1/postings/import`, and let a human press submit.

---

## 15. Related documents

| Document | Covers |
|---|---|
| `ARCHITECTURE.md` | The invariants themselves, the pipeline, the human steps |
| `SECURITY_ARCHITECTURE.md` | How each control is technically enforced: the deny-list constant, the SSRF gate, secret handling, the artifact trigger |
| `SOURCE_ADAPTERS.md` | The adapter protocol, per-source endpoints and caveats, `assert_fetch_allowed`, robots handling, the UA policy, rate-limit buckets, the `mail_alert` distinction, the deferred adapters |
| `COMPANY_REGISTRY.md` | The detection flow and the never-scrape refusal path |
| `EMAIL_INGESTION.md` | Gmail scopes and the reasoning, the single-recipient send rule, the privacy posture |
| `DATA_MODEL.md` | `email_message` without a body column, retention-relevant columns, soft-delete policy |
| `API.md` | The deliberately absent endpoints |
| `AI_ARCHITECTURE.md` | Untrusted-content handling for job descriptions and mail |
| `APPLICATION_PIPELINE.md` | `source_channel`, referral tracking, the funnel |
