# BruceTech Facebook Lead Pipeline

Scrapes public Facebook group posts via Apify, deduplicates and imports them
into Airtable, then qualifies them for BruceTech outreach.

Two rules define this pipeline.

**The AI does not decide who is a lead.** It extracts structured observations
about a post. Python applies the scoring and qualification rules
deterministically, so the same post always produces the same decision and
every rule is unit-testable.

**No solution hopping.** A business owner having a problem is not the same
thing as a BruceTech sales opportunity. The pipeline asks whether the author
expressed a need that *directly* maps to something BruceTech provides — not
whether BruceTech could theoretically offer an alternative. "I need a virtual
assistant" is a real request for a provider BruceTech is not. It is rejected.

---

## Architecture

```
Apify task
      │
      ▼
Resolve ONE exact run ───────────► Facebook Post Scraper Runs
  run ID · dataset ID · cost              (upsert by Apify Run ID)
      │                                            │
      ▼                                            │
That run's dataset — and no other                  │
      │                                            │
      ▼                                            │
Deduplication ─ post ID · canonical URL · fingerprint · author
      │                                            │
      ▼                                            ▼
Airtable import ─────────── Apify Run ID + Scraper Run link
      │
      ▼
Deterministic intent classification (Python)
      │   Provider · Implementation · Business Pain
      │   Tool Research · General Advice · Unrelated
      ▼
Word-boundary service matching (Python)
      │   named · described (operational failure) · adjacent (growth)
      │   physical/clinical goods → no weak match
      ▼
Prefilter Score ─── gates the AI call, orders the queue
      │             (general advice and unrelated stop here, for free)
      ▼
Non-overridable gate ─── stale · no service match · promotional ·
      │                  job seeker · resolved · too short → rejected free
      ▼
Prioritised AI queue ─── Approve › this run › provider › implementation ›
      │                  prequalified › score › fresh › quiet
      ▼
AI signal extraction ─── 24 structured signals, no score, no verdict
      │
      ▼
Deterministic qualification (Python)
      │  score → tier → hard rejections → Qualified → Outreach Ready
      ▼
Airtable write-back ─── + Qualification Version
      │
      ▼
Facebook Group Performance ─── scraped → AI candidates → qualified →
                               outreach → contacted → replied →
                               meeting → proposal → won
```

### Files

| File | Purpose |
|---|---|
| `run_pipeline.py` | Orchestration, Airtable, OpenAI, queue, write safety |
| `qualification.py` | Scoring, tiers, hard rejections, prefilter. Pure logic |
| `intent.py` | Intent classification and service matching. Pure logic |
| `apify_runs.py` | Exact Apify run resolution and dataset retrieval |
| `scraper_runs.py` | Idempotent scraper-run logging in Airtable |
| `group_performance.py` | Per-group operational metrics |
| `normalization.py` | URL canonicalisation, fingerprinting, deduplication |
| `tests/` | Regression suite (no credentials required) |

`qualification.py`, `intent.py`, and `normalization.py` deliberately have no
environment, network, or third-party dependencies. `apify_runs.py`,
`scraper_runs.py`, and `group_performance.py` take injected callables for every
external call, so all six are testable in isolation.

---

## Qualification rules

### Scoring

The AI returns signals; Python converts them to a 0–100 score. Component
maximums sum to exactly 100:

| Component | Max | Values |
|---|---:|---|
| `intent_strength` | 28 | none 0 · weak 9 · moderate 19 · strong 28 |
| `service_match` | 20 | 16 for one category, +2 per extra, capped at 20 |
| `problem_specificity` | 12 | none 0 · vague 4 · specific 9 · detailed 12 |
| `business_context` | 12 | none 0 · unclear 4 · sole trader 8 · small 11 · established 12 |
| `purchase_signal` | 10 | none 0 · researching 4 · comparing 8 · ready 10 |
| `urgency` | 6 | none 0 · low 2 · medium 4 · high 6 |
| `business_impact` | 6 | none 0 · low 2 · medium 4 · high 6 |
| `location` | 3 | unknown/other 0 · canada 2 · ontario/GTA 3 |
| `buyer_role` | 3 | unknown/other 0 · employee 1 · manager 2 · owner 3 |

Soft penalties (these reduce the score but never reject on their own):

| Penalty | Points |
|---|---:|
| `resolved_status = likely_resolved` | −15 |
| `spam_risk = medium` | −12 |
| `spam_risk = low` | −4 |
| `outreach_appropriateness = borderline` | −10 |
| `classification_confidence < 0.5` | −8 |

The total is clamped to 0–100.

### Tiers

| Tier | Score | Qualified | DM |
|---|---|---|---|
| **Hot** | ≥ 80 | ✅ true | if personalised |
| **Qualified** | 65–79 | ✅ true | if personalised |
| **Manual Review** | 55–64 | ❌ false | never |
| **Rejected** | < 55, or any hard rejection | ❌ false | never |

Only Hot and Qualified may set `Qualified = true`.

Manual Review records are written with `Qualified = false`, no DM,
`Manual Review = true`, and a `[MANUAL REVIEW]` prefix on the summary so they
are obvious in the table.

### Hard rejections

A hard rejection **always** overrides the numeric score. A post scoring 100
that trips any of these is Rejected.

| Code | Trigger |
|---|---|
| `PERSONAL_REQUEST` | Personal consumer request |
| `PROMOTIONAL_POST` | Service provider advertising itself |
| `COMPETITOR_OR_AGENCY` | Agency or freelancer promoting themselves |
| `JOB_SEEKER` | Job seeker or person offering labour |
| `SPAM_OR_AFFILIATE` | Spam or affiliate promotion (`spam_risk = high`) |
| `STUDENT_OR_EDUCATIONAL` | Student or coursework question |
| `FREE_ONLY_REQUEST` | Asking for free work only |
| `ALREADY_RESOLVED` | Request already resolved |
| `PROVIDER_ALREADY_SELECTED` | A provider has been chosen |
| `NO_BUSINESS_CONTEXT` | No credible business context |
| `NO_SERVICE_MATCH` | No BruceTech service match |
| `INAPPROPRIATE_OUTREACH` | Outreach would be unwelcome |
| `STALE_POST` | Older than `MAX_POST_AGE_DAYS` |
| `NO_BUYING_INTENT` | Intent is General Advice or Unrelated |
| `FUNDING_OR_FINANCE_REQUEST` | Financing / credit question with no service need |
| `HIRING_UNRELATED` | Staffing question unrelated to BruceTech |
| `SUPPRESSED_AUTHOR` | Author is in `SUPPRESSED_AUTHORS` |
| `DISALLOWED_GROUP` | Group is in `DISALLOWED_GROUPS` |
| `HUMAN_REJECTED` | A human set `Human Decision = Reject` |
| `POST_TOO_SHORT` | Not enough text to evaluate |

The last eight are decided in Python and are **never offered to the model** —
they are absent from the JSON schema enum. `STALE_POST` in particular is
computed from the post timestamp and never trusted from the model.

Only `NO_BUYING_INTENT`, `FUNDING_OR_FINANCE_REQUEST`, and `HIRING_UNRELATED`
can be lifted by a human — they are the heuristic guesses a reviewer can see
are wrong by reading the post. Every other code in this table is in
`NON_OVERRIDABLE_HARD_REJECTIONS` and stands regardless of who approved what.

### Qualified vs Outreach Ready

These answer different questions and are not interchangeable.

**Qualified** means *strong commercial and service fit*. It is a property of
the opportunity.

**Outreach Ready** means *it is appropriate to contact this person now*. It is
strictly narrower, and requires **all** of:

1. Tier is Hot or Qualified
2. No hard rejection applies
3. The AI wrote a specific, personalised DM
4. `recommended_channel` is not `do_not_contact`
5. `classification_confidence` ≥ `MIN_OUTREACH_CONFIDENCE` (default `0.55`)
6. Intent is Provider Request, Implementation Request, or Business Pain
7. For Business Pain only: score ≥ `BUSINESS_PAIN_OUTREACH_MIN_SCORE`
   (default `70`)

Rule 6 is what stops a research question becoming a sales DM. Rule 7 exists
because business pain is *inferred* — the author described a problem but never
asked for a provider — so the bar for a cold message is higher than for
someone who explicitly asked to hire.

| Intent | Can be Qualified | Can be Outreach Ready |
|---|---|---|
| Provider Request | ✅ | ✅ |
| Implementation Request | ✅ | ✅ |
| Business Pain | ✅ | ✅ at score ≥ 70 |
| Tool Research | ✅ | ❌ — Manual Review |
| General Advice | ❌ | ❌ — hard rejected |
| Unrelated | ❌ | ❌ — hard rejected |

There is **no fallback DM**. If the AI cannot write a specific message, the
record stays qualified but is not outreach-ready, and `Suggested DM` is blank.
A `do_not_contact` recommendation is respected and never upgraded.

---

## Intent classification

Every post is classified in Python before any AI call. Intent and service
match are independent axes, and the prefilter requires **both**.

| Intent | Examples | Reaches AI | Outreach |
|---|---|---|---|
| **Provider Request** | "looking for someone", "can anyone recommend a developer", "looking to hire" | ✅ | ✅ |
| **Implementation Request** | "need help setting this up", "need our website rebuilt", "need payments integrated" | ✅ | ✅ |
| **Business Pain** | "missing calls", "still using pen and paper", "answering texts all evening" | ✅ | ✅ at score ≥ 70 |
| **Tool Research** | "what CRM do you use", "has anyone tried X", "which booking platform" | ✅ | ❌ |
| **General Advice** | "how do I get financing", "should I hire an employee", "what supplier" | ❌ | ❌ |
| **Unrelated** | no credible BruceTech connection | ❌ | ❌ |

Classification runs in that order and the first match wins. Provider outranks
implementation, so "looking for someone to set up our CRM" is a hiring request
that happens to name a product. Tool research outranks business pain, so
"what CRM does everyone use, I keep missing calls?" is research.

### Service matching

Terms are matched with **word-boundary-anchored regex**, never substrings.

```python
"api" in "working capital"                  # True  — the old bug
intent.compile_term("api").search("...")    # None  — the fix
```

Two tiers:

- **Strong terms** are unambiguous — `wordpress`, `crm`, `api`, `microsoft 365`,
  `gohighlevel`, `pen and paper`. One hit is a service match.
- **Weak terms** are real BruceTech vocabulary that also appears in ordinary
  business talk — `network`, `security`, `payment`, `software`, `system`. A
  weak term matches only with corroboration: **two distinct weak categories**,
  or **one weak term plus technical context** ("set up", "broken", "migrate").

So "our office network keeps dropping" is a managed IT lead, while "trying to
grow my network" and "is a security deposit normal?" are not.

---

## Prefilter Score

A deterministic 0–100 **prioritisation** number computed from the raw post
before the model sees anything. It is not the Lead Score. Its only jobs are to
gate the AI call and order the queue.

| Component | Points |
|---|---|
| Intent — Provider Request | 35 |
| Intent — Implementation Request | 32 |
| Intent — Business Pain | 22 |
| Intent — Tool Research | 10 |
| Intent — General Advice / Unrelated | 0 |
| Service — strong term | 18, +4 per extra category, capped at 26 |
| Service — corroborated weak term | 8 |
| Freshness — under 24 h | 18 |
| Freshness — 1–3 days | 12 |
| Freshness — 3–5 days | 6 |
| Freshness — within max age but older | 2 |
| Freshness — no usable timestamp | 4 |
| Commercial context — 2+ phrases | 10 |
| Commercial context — 1 phrase | 6 |
| Length — ≥ 400 / ≥ 180 / ≥ 80 chars | 8 / 6 / 3 |
| Competition — 0–5 comments | +8 |
| Competition — 6–20 comments | +4 |
| Competition — 21–50 comments | 0 |
| Competition — 51+ comments | −6 |
| Penalty — promotional | −45 |
| Penalty — job seeking | −40 |
| Penalty — already resolved | −35 |
| Penalty — free work only | −25 |
| Penalty — no service match | −20 |

Clamped to 0–100. A post passes the prefilter only when it has a service
match, an AI-eligible intent, no promotional / job-seeking / resolved
language, and a score of at least `PREFILTER_PASS_SCORE` (default `30`).

Comment volume is a **prioritisation** signal, not a rejection. A strong lead
in a busy thread still reaches the model; it just sorts lower.

### Three ways to match a service

A post can establish BruceTech fit three ways. The basis is recorded on every
record as `match_basis`, so an operator can tell a stated requirement from an
inferred one.

| Basis | Rule | Example |
| --- | --- | --- |
| `named` | The post uses BruceTech vocabulary | "the whole GoHighLevel setup" |
| `described` | It describes an operational systems failure, from a business with commercial context | "200 units missing and no systems in place" |
| `adjacent` | It asks a provider for measurable client acquisition, which BruceTech serves through the site, SEO, booking flow and CRM underneath | "a marketing agency to get me patients" |

`described` and `adjacent` never produce a *strong* match. They credit the
categories BruceTech would actually deliver and lean on the model for the
commercial judgement — an `adjacent` match is flagged `needs_ai_confirmation`,
and `NO_SERVICE_MATCH` still applies to whatever the model returns. Both
require commercial context; neither is available to an anonymous post.

A request naming only creative or physical-media work — influencers, content
creators, photographers, PR, branding, print — is excluded from `adjacent`
unless it also names something BruceTech builds.

### The physical-goods guard

BruceTech does not sell, service, or advise on physical clinical equipment. A
post shopping for goods gets no weak-signal service match, whatever
vocabulary it happens to contain:

> "Looking at buying a laser device… the Candela vs Cynosure **platforms** for
> a small **clinic**"

Two corroborating weak signals, and not a lead. IT hardware is deliberately
outside the guard — a dead printer or a crashed workstation is BruceTech work.

### Promotion needs a call to action

Describing what your business sells is not advertising it. "We offer facials,
DiamondGlow and SkinPen, but her books are half empty" is a business owner
giving context for a problem.

`PROMOTIONAL_POST` requires seller-to-audience evidence — "DM me", "book now",
"limited time", "for sale", "free consultation". Self-description alone
("we offer", "our services", "we provide") is recorded in the diagnostics and
rejects nothing on its own.

---

## The AI queue

Each run fetches candidates in three phases:

1. **Every** record where `Human Decision = Approve` and `AI Status` is blank
   or `Pending`.
2. **Every** record where `Prequalification = Send to AI` and `AI Status` is
   blank or `Pending`.
3. The newest remaining unprocessed records, sorted server-side by post
   time, to fill the window (`AI_BATCH_LIMIT × 5`).

Phases 1 and 2 exist so neither your approvals nor the formula's selection is
lost behind an arbitrary page of the backlog. Phase 1 ignores
`REQUIRE_AIRTABLE_PREQUALIFICATION`: that flag narrows which machine-eligible
records are worth reading, and you have already answered that question by
approving the record. Phase 3 is sorted because truncating an unsorted
Airtable query returns records in table order, not by relevance.

**`Prequalification` decides what is looked at, never what is sent to the
model.** It is an Airtable formula computed from `Request Signal`,
`Service Signal`, and `Promotion Signal`. It knows nothing about post age,
intent, or human review, and it grants a record nothing: every candidate from
either phase goes through the same deterministic prefilter afterwards.

With `RETRY_AI_ERRORS` enabled (the default), records whose last AI attempt
failed are re-queued too — see [AI errors](#ai-errors-and-recovery).

### Queue order

1. Records a human set `Human Decision = Approve` on
2. Records imported during this run
3. Provider requests
4. Implementation requests
5. Records `Prequalification` marks `Send to AI`
6. Highest Prefilter Score
7. Newest post time
8. Least comment competition

Intent outranks recency deliberately. A three-day-old "looking for someone to
rebuild our site" is worth more than a fresh vague grumble, and the previous
recency-first ordering let a stale backlog consume the whole `AI_BATCH_LIMIT`
before the day's real leads were reached.

### Records the queue skips entirely

- **Already in the sales funnel.** Any record whose `Outreach Status` is
  anything other than blank or `Not Contacted` is skipped, so a
  classification run can never overwrite a recorded sales outcome.
- **Retry budget spent.** A record that has failed `AI_ERROR_MAX_ATTEMPTS`
  times is retired from the queue.
- **Rejected by a human.** `Human Decision = Reject` ends it there. The model
  is not asked to argue with a person who has already decided.
- **Blocked by a non-overridable rejection** — see below.

### The non-overridable pre-AI gate

Before the queue, before any override, and before the OpenAI client is even
constructed, a record carrying any of these is rejected deterministically:

| Code | Meaning |
| --- | --- |
| `STALE_POST` | Older than `MAX_POST_AGE_DAYS` |
| `NO_SERVICE_MATCH` | Nothing in it is a BruceTech job |
| `PROMOTIONAL_POST` | A provider advertising themselves |
| `JOB_SEEKER` | Someone offering their labour |
| `ALREADY_RESOLVED` | The request has already been answered |
| `POST_TOO_SHORT` | Not enough text to evaluate |

Nothing lifts these. Not `Human Decision = Approve`, not the
`Prequalification` formula, not `ENFORCE_PYTHON_PREFILTER=false` — that
switch turns off the *heuristic* prefilter, and age is not a heuristic.

This exists because of the 2026-08-18 production run: six records the
Airtable formula marked `Send to AI` skipped the prefilter, and five posts
between 17 and 25 days old were sent to `gpt-5-mini` and rejected as
`STALE_POST` after the call had been paid for.

### `Human Decision = Approve` overrides the prefilter

`Approve` is the **only** human override in the pipeline. It sorts a record
above everything else — including records imported in the current run — and
lets it past the prefilter when the only thing standing in the way is a
heuristic: the intent classification, or a Prefilter Score below the minimum.

At decision time it lifts exactly `NO_BUYING_INTENT`,
`FUNDING_OR_FINANCE_REQUEST`, and `HIRING_UNRELATED`. It lifts nothing else,
and it never makes a lead outreach-ready on its own — you are already in the
loop and will see the result.

`Review` is not a decision. It changes nothing about filtering.

Every other record that fails the prefilter is marked
`AI Status = Processed`, `Lead Tier = Rejected` **without an AI call**, and
is not evaluated again. That is intended behaviour: it keeps the backlog
from growing and keeps token spend on plausible leads. It also means a
borderline post can be rejected on keywords alone — set
`Human Decision = Approve` if you want the model to decide instead.

Set `ENFORCE_PYTHON_PREFILTER=false` to send everything else to the AI. The
non-overridable gate above still applies.

---

## Environment variables

Secrets — set as GitHub Actions secrets, never committed:

| Variable | Purpose |
|---|---|
| `APIFY_TOKEN` | Apify API token |
| `APIFY_TASK_ID` | Apify task to read or run |
| `AIRTABLE_TOKEN` | Airtable personal access token |
| `AIRTABLE_BASE_ID` | Airtable base ID |
| `AIRTABLE_TABLE_NAME` | Raw Signals table name |
| `OPENAI_API_KEY` | OpenAI API key |

Qualification:

| Variable | Default | Purpose |
|---|---|---|
| `QUALIFICATION_THRESHOLD` | `65` | Minimum score to qualify |
| `MANUAL_REVIEW_THRESHOLD` | `55` | Minimum score for manual review |
| `HOT_LEAD_THRESHOLD` | `80` | Minimum score for the Hot tier |
| `MAX_POST_AGE_DAYS` | `5` | Posts older than this are rejected |
| `QUALIFICATION_VERSION` | `facebook-v2` | Stamped on every evaluated record |

Prefilter and outreach:

| Variable | Default | Purpose |
|---|---|---|
| `ENFORCE_PYTHON_PREFILTER` | `true` | Skip the AI for obvious rejects |
| `PREFILTER_PASS_SCORE` | `30` | Minimum Prefilter Score to reach the AI |
| `ALLOW_TOOL_RESEARCH_TO_AI` | `true` | Send research posts to the AI (Manual Review only) |
| `MIN_OUTREACH_CONFIDENCE` | `0.55` | Confidence floor for automatic outreach |
| `BUSINESS_PAIN_OUTREACH_MIN_SCORE` | `70` | Score bar for business-pain outreach |
| `SUPPRESSED_AUTHORS` | *(empty)* | Comma-separated author names to never contact |
| `DISALLOWED_GROUPS` | *(empty)* | Comma-separated group titles to exclude |
| `REQUIRE_AIRTABLE_PREQUALIFICATION` | `false` | Also require the Airtable formula |

Apify run attribution:

| Variable | Default | Purpose |
|---|---|---|
| `RUN_APIFY_TASK` | `false` | Start a fresh task run and await that exact run |
| `APIFY_RUN_ID` | *(empty)* | Import from one exact run. Overrides `RUN_APIFY_TASK` |
| `APIFY_RUN_TIMEOUT_SECONDS` | `900` | Max wait for a run to finish |
| `APIFY_POLL_INTERVAL_SECONDS` | `15` | Poll interval while waiting |
| `LOG_SCRAPER_RUNS` | `true` | Upsert the Facebook Post Scraper Runs record |
| `AIRTABLE_SCRAPER_RUNS_TABLE` | `Facebook Post Scraper Runs` | Run log table name |

`APIFY_START_NEW_RUN` and `APIFY_RUN_POLL_SECONDS` remain accepted as aliases
for `RUN_APIFY_TASK` and `APIFY_POLL_INTERVAL_SECONDS`.

AI processing and recovery:

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_MODEL` | `gpt-5-mini` | Model used for signal extraction |
| `OPENAI_MAX_OUTPUT_TOKENS` | `2500` | Output token ceiling |
| `OPENAI_MAX_ATTEMPTS` | `3` | Retries per post, within one run |
| `OPENAI_RETRY_DELAY_SECONDS` | `5` | Base retry delay |
| `AI_BATCH_LIMIT` | `20` | Max AI calls per run |
| `MAX_POST_CHARS` | `8000` | Post text truncation |
| `RETRY_AI_ERRORS` | `true` | Re-queue records whose last attempt failed |
| `AI_ERROR_MAX_ATTEMPTS` | `3` | Total attempts per record, across runs |

Reporting and safety:

| Variable | Default | Purpose |
|---|---|---|
| `UPDATE_GROUP_PERFORMANCE` | `true` | Refresh group metrics after each run |
| `AIRTABLE_GROUP_PERFORMANCE_TABLE` | `Facebook Group Performance` | Metrics table name |
| `AIRTABLE_FORMULA_WAIT_SECONDS` | `15` | Pause after import |
| `DRY_RUN` | `false` | Read and score, but write nothing and start no paid run |

All are read lazily. Importing `run_pipeline` requires no credentials, which
is what lets the test suite run in CI without secrets.

---

## Apify run attribution

Every imported post is tied to exactly one Apify run.

The pipeline resolves a **run object** before touching any dataset, then reads
that run's own `defaultDatasetId`. It never uses the
`/runs/last/dataset/items` shortcut, which returns items with no run identity
attached — the source of the old attribution bug, where a concurrent manual
Apify run could silently substitute its posts.

Three modes:

| Mode | Trigger | Behaviour |
|---|---|---|
| **Pinned** | `APIFY_RUN_ID` set | Use exactly that run. Waits if it is still going; fails if it did not succeed |
| **Start** | `RUN_APIFY_TASK=true` | Start the task, then poll *that run's ID* to a terminal state |
| **Resolve** | default | Fetch the task's last successful run **as a run object** |

The resolved run is upserted into **Facebook Post Scraper Runs**, keyed on
`Apify Run ID`, so re-running against the same scrape never creates a
duplicate log. Every Raw Signal imported from it is written with `Apify Run ID`
and a `Scraper Run` linked record.

Cost and Run Input are written **only when Apify actually supplies them**.
`usageTotalUsd` is returned only to the token that owns the run, and some
actors store no `INPUT` record; in either case the column is left untouched
rather than filled with a guess.

---

## AI errors and recovery

A record that fails is not lost forever, and does not loop forever either.

- **Within one run**, `OPENAI_MAX_ATTEMPTS` retries with a growing delay.
- **Across runs**, a record marked `Error` is re-queued while
  `RETRY_AI_ERRORS` is true, up to `AI_ERROR_MAX_ATTEMPTS` total attempts.
- **Transient failures** — timeouts, rate limits, 5xx, connection resets —
  consume one attempt.
- **Permanent failures** — a malformed record, an impossible schema state —
  consume the whole budget at once so they never come back.

The attempt count is stored as JSON in `AI Output`, and only on records that
actually failed. Real model output is never replaced by exception text, and a
failure never blanks `Lead Score`, `Qualified`, or `Suggested DM`. The live
schema has no dedicated attempt field; see `docs/PIPELINE_AUDIT.md` for the
limitation this creates.

---

## Human decisions and sales outcomes

The pipeline **reads** these fields and never writes them:

| Field | Effect on the pipeline |
|---|---|
| `Human Decision` | `Approve` is the only human override: it lifts the intent heuristics and nothing else. `Reject` stops the record before the AI call and adds `HUMAN_REJECTED`. `Review` changes nothing |
| `Outreach Status` | Anything past `Not Contacted` makes the record skip classification entirely |
| `Last Contacted` | Read only |
| `Contacted` (legacy checkbox) | Read only; counted as contacted when `Outreach Status` is empty |

`run_pipeline.strip_read_only_fields` is applied to every Airtable payload
before it is sent, so a formula field or a human-owned field cannot be written
even by a coding mistake.

---

## Facebook Group Performance

Refreshed after each run from Raw Signals, which are the source of truth for
operational counts.

**Never written:** `Tier`, `Status`, `Revenue`, `Notes`. Those are human
strategy and sales data. A group's Tier influences scraper planning only — it
can never make a bad post qualify, and a new group can never suppress a strong
provider request. Post-level evidence is always primary.

| Metric | Definition |
|---|---|
| Posts Scraped | Unique Raw Signals for the group |
| AI Candidates | Records with non-blank `Evidence` (only written from model output) |
| System Qualified | `Qualified = true` |
| Provider Requests | Intent recomputed in Python from the post text |
| Outreach Ready | `Outreach Ready = true` |
| Contacted | `Outreach Status` past `Not Contacted`, or legacy `Contacted` when the status is blank |
| Replies | Replied, Meeting Booked, Proposal Sent, Won, Lost |
| Meetings | Meeting Booked, Proposal Sent, Won |
| Proposals | Proposal Sent, Won |
| Won | Won |
| Last Run | Latest `Run Date` across linked scraper runs |

`Lost` counts as contacted and replied but **not** as a meeting or proposal:
a deal can be lost at any stage, so inferring a meeting from it would inflate
the funnel.

Records with no `Scraper Run` link — everything imported before run
attribution existed — simply contribute nothing to `Last Run`. Historical
links are never fabricated. Groups with activity but no performance record are
reported in the log, not auto-created.

---

## Airtable fields

Base: **Bruce Tech**. Tables: `Facebook Raw Signals`,
`Facebook Post Scraper Runs`, `Facebook Group Performance`.

### Facebook Raw Signals

Source fields, written on import:

`Url` · `Facebook url` · `Time` · `User ID` · `User name` · `Text` ·
`Group title` · `Input Url` · `Likes count` · `Comments count` ·
`Shares count` · `Apify Run ID` · `Scraper Run`

Qualification fields, written after processing:

`AI Status` · `Qualified` · `Lead Score` · `Service Match` · `Lead Summary` ·
`Rejection Reason` · `Suggested DM` · `AI Output` · `Recommended channel` ·
`Evidence` · `Lead Tier` · `Manual Review` · `Outreach Ready` ·
`Disqualifiers` · `Prefilter Score` · `Qualification Version`

**Read only — never written by the pipeline:**

| Field | Why |
|---|---|
| `Request Signal` · `Service Signal` · `Promotion Signal` · `Prequalification` | Airtable formulas |
| `Provider Intent Signal` · `Research Intent Signal` · `Intent Type` | Airtable formulas |
| `Human Decision` · `Outreach Status` · `Last Contacted` · `Contacted` | Human-owned |

Because `Intent Type` is a formula, the Python intent classification is
recorded inside the `AI Output` JSON blob rather than in its own column.

### Facebook Post Scraper Runs

`Run Name` · `Run Input` · `Results` · `Cost` · `Duration` · `Run Date` ·
`Apify Run ID` · `Dataset URL`

`Apify Run ID` is the idempotency key — the same Apify run is never logged
twice.

### Facebook Group Performance

`Group Name` · `Group URL` · `Posts Scraped` · `AI Candidates` ·
`System Qualified` · `Provider Requests` · `Outreach Ready` · `Contacted` ·
`Replies` · `Meetings` · `Proposals` · `Won` · `Last Run`

`Tier`, `Status`, `Revenue`, and `Notes` are human-owned and never written.

### Schema validation

Before any production run the pipeline reads the Airtable metadata API and
checks that every required Raw Signals field exists. A missing field **fails
the run with a message naming it**, before any record is modified.

If the token lacks the `schema.bases:read` scope the check is skipped with a
note, and the write path falls back to writing only the core fields with a
warning. The pipeline never creates, renames, or deletes an Airtable field.

> **Outreach Ready views.** Filter on `Outreach Ready = checked`, not on
> `Qualified`. Qualified leads without a personalised message, and research
> posts, are intentionally not outreach-ready.

---

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export APIFY_TOKEN=...
export APIFY_TASK_ID=...
export AIRTABLE_TOKEN=...
export AIRTABLE_BASE_ID=...
export AIRTABLE_TABLE_NAME=...
export OPENAI_API_KEY=...

python run_pipeline.py
```

To start a fresh Apify run and use that exact run's dataset instead of the
last successful one:

```bash
RUN_APIFY_TASK=true python run_pipeline.py
```

To re-import one specific run — useful for reproducing a bad import:

```bash
APIFY_RUN_ID=<apify run id> python run_pipeline.py
```

Never commit real credentials. Use environment variables or a local `.env`
file that is git-ignored.

---

## Testing against your real Airtable data

`DRY_RUN=true` reads Apify and Airtable and calls the AI exactly as normal,
but makes **no Airtable writes** and **starts no paid Apify run** — with
`RUN_APIFY_TASK=true` it reads the last successful run instead. It prints the
decision it would have written for each record:

```
[DRY RUN] Would update recABC123: tier=Qualified score=71 qualified=True outreach=True dm=yes
[DRY RUN] Would update recDEF456: tier=Manual Review score=58 qualified=False outreach=False dm=no
```

A safe first pass over your existing base:

```bash
export AIRTABLE_BASE_ID=...        # your real base
export AIRTABLE_TABLE_NAME=...
export AIRTABLE_TOKEN=...
export OPENAI_API_KEY=...
export APIFY_TOKEN=...
export APIFY_TASK_ID=...

DRY_RUN=true AI_BATCH_LIMIT=5 python run_pipeline.py
```

`AI_BATCH_LIMIT=5` keeps the OpenAI spend to five posts.

### Records already processed will be skipped

The queue only picks up records whose `AI Status` is blank or `Pending`.
Everything the old pipeline processed is marked `Processed` and will be
ignored, so a dry run against an existing base may find nothing to do.

To re-score records you have already seen, clear `AI Status` on a sample of
them in the Airtable UI (select the cells and delete), then run again. Start
with 5–10 rows that you already have an opinion about, so you can compare the
new tier against your own judgement.

Note that re-scoring costs one OpenAI call per record — the new rules need
the structured signals, which cannot be derived from the old `Lead Score`.

### When you are ready to write

Duplicate the base first if you want a guaranteed-safe target
(**Airtable → base menu → Duplicate base**), point `AIRTABLE_BASE_ID` at the
copy, and drop `DRY_RUN`. Otherwise run against the real base with a small
`AI_BATCH_LIMIT` and check the results before raising it.

Remember that a real run **overwrites** `Qualified`, `Lead Score`,
`Suggested DM`, `Rejection Reason`, and `AI Status` on every record it
processes.

---

## Running tests

```bash
pip install -r requirements-dev.txt

python -m compileall -q .    # compilation check
python -m pytest -q          # full suite
```

The suite needs no API keys, no network, and no Airtable base. No test starts
a paid Apify run, writes to Airtable, or makes an OpenAI request — every
external call is mocked.

It covers:

- the 65-point threshold, including the 64 / 65 boundary
- every hard rejection condition, and hard rejections overriding a perfect score
- tier boundaries and the Qualified-only-for-Hot/Qualified rule
- the absence of any fallback DM, and `do_not_contact` never being upgraded
- **`"working capital"` never matching `api`**, and `api` still matching
  "integrating an API"
- short tokens (`pos`, `seo`, `ai`) never matching inside longer words
- intent classification across all six types, including the five production
  fixtures in `tests/fixtures.py`
- service match required before AI eligibility; promotional and job-seeking
  posts never reaching the AI
- exact Apify run attribution, including that a concurrent run cannot
  substitute its dataset
- scraper-run upsert by `Apify Run ID`, and no duplicate log for the same run
- every imported Raw Signal receiving `Apify Run ID` and a `Scraper Run` link
- group performance counts, and `Tier` / `Status` / `Revenue` / `Notes` never
  appearing in a payload
- `Human Decision` and `Outreach Status` never being written
- bounded AI error retries, transient vs permanent
- URL normalisation and duplicate detection
- queue prioritisation ordering

---

## Deployment

The pipeline runs from GitHub Actions
(`.github/workflows/facebook-leads.yml`).

1. Add the six secrets under **Settings → Secrets and variables → Actions**.
2. Merge to `main`.

> **The daily schedule is currently paused.** The `schedule:` block is
> commented out. Uncomment the three lines to restore the 06:30
> America/Toronto run.

Until then the pipeline runs only via **Run workflow**, which offers four
inputs: **Dry run**, **Start new Apify run**, **AI batch limit**, and
**Apify run ID**.

Two jobs run in sequence: `test` (compile + pytest) then `run-pipeline`. The
pipeline only runs if the tests pass.

A `concurrency` group named `facebook-leads-pipeline` guarantees two runs
never overlap. `cancel-in-progress` is deliberately `false` so an in-flight
run finishes its Airtable writes rather than being killed mid-batch.

---

## Expect fewer leads

This release rejects substantially more than its predecessor, on purpose. The
goal is high-intent leads per AI call, not AI calls per scraper run.

Five changes each reduce volume:

1. General advice and unrelated posts are hard-rejected on intent
2. Service matching is word-boundary strict, so the old false positives are gone
3. `MAX_POST_AGE_DAYS` dropped from 45 to 5
4. Outreach needs `classification_confidence` ≥ 0.55
5. Business-pain leads need a score of 70, not 65, before automatic outreach

Records carrying `Qualification Version = facebook-v2` are the ones scored
under these rules. Historical records keep their legacy scores and are never
mass-reprocessed, so the two populations stay comparable.
