# Changelog

All notable changes to the BruceTech Facebook lead pipeline.

## [Unreleased] — Intent classification, run attribution, and precision

Branch: `agent/facebook-pipeline-v2-intent-attribution`
Qualification version: `facebook-v2`

This release does two things: it stops the pipeline treating "a business owner
has a problem" as "a BruceTech sales opportunity", and it makes every imported
post traceable to the exact Apify run that produced it.

The previous release's deterministic qualification work is preserved intact —
the 65-point threshold, the scoring weights, the tiers, the hard rejections,
and all 125 of its tests are unchanged.

### Fixed — qualification recall was too low to find real leads

Found by the first fresh scrape, Apify run `Q3Ix6zmHrEDhgiQGf` (dataset
`rVjOEFf81ZIT23RWl`, 50 posts) on 2026-08-19. Attribution and scraper-run
logging were correct — all 50 Raw Signals carried the run ID and linked to the
right Scraper Run record — but effectively nothing reached the model, several
credible business problems and two explicit provider requests included.

Four recall defects, each fixed narrowly:

**Operational failure described in plain English was invisible.** A med spa
manager with "200 units missing" and "no systems in place" classified as
`UNRELATED` with no service match. `OPERATIONS_PAIN_PATTERNS` now recognises
absent systems, inventory and reconciliation failures, and scheduling or
booking breakdowns — as *problems*, never as nouns. "Inventory" alone matches
nothing. Gated on commercial context, so a consumer grumble stays `UNRELATED`.

**"We offer" made a post promotional.** Describing what a business sells is
not advertising it. `PROMOTIONAL_TERMS` now holds only seller-to-audience
evidence — "DM me", "book now", "limited time", "for sale". Self-description
moved to `PROMOTIONAL_SELF_DESCRIPTION_TERMS` and is promotional only
alongside a call to action; on its own it is recorded in the diagnostics and
rejects nothing.

**Marketing provider requests had no path.** "A marketing agency to get me
patients" is not a website request, but digital client acquisition is
delivered through the site, SEO, booking flow, CRM, and follow-up automation
underneath it. A narrow `adjacent` path requires an explicit provider ask,
a measurable acquisition goal, and commercial context; it credits only the
categories BruceTech would deliver, flags itself `needs_ai_confirmation`, and
leaves real fit to the model. Requests for influencers, content creators,
photographers, PR, branding, or print media are excluded outright unless
something BruceTech builds is named alongside.

**Software comparisons were not tool research.** "Mangomint or boulevard POS
system and why?" classified as `UNRELATED`. Named either/or over a *software*
noun, and switching language, now classify as `TOOL_RESEARCH` — which is
AI-eligible but never auto-contacted.

The counterweight is a physical-goods guard. A medical spa group discusses
lasers, syringes, and treatment chairs constantly, and those posts are full of
words like "system", "platform", and "device". A post shopping for physical or
clinical goods now gets no weak-signal service match at all. IT hardware is
deliberately outside the guard: a dead printer is BruceTech work.

Every record now records `match_basis` — `named`, `described`, `adjacent`, or
`none` — so an operator can tell a stated requirement from an inferred one.

No scoring weight, threshold, or gate changed. The stale gate, `Human
Decision` behaviour, deduplication, attribution, retry architecture, and write
protection are untouched, each asserted by a test.

### Changed — operator-visible Airtable intent formulas

`airtable_formulas.py` holds pasteable sources for `Provider Intent Signal`,
`Research Intent Signal`, and `Intent Type`, closing the gaps the fresh run
showed: a marketing-agency provider request read as "Other" with
`Provider Intent Signal = 0`.

These decide nothing. `intent.py` remains authoritative, a test asserts
`run_pipeline` never imports the module, and applying them to the base is a
manual step the pipeline does not depend on.

### Fixed — an Airtable formula was acting as human approval, and stale posts were reaching the model

Found by the first production run of this branch,
[32198160409](https://github.com/brucetechnicalservices-collab/BT-facebook-lead-pipeline/actions/runs/32198160409)
on 2026-08-18. Five posts dated 2026-07-24 to 2026-08-01 — 17 to 25 days old
against `MAX_POST_AGE_DAYS=5` — were sent to `gpt-5-mini`. All five came back
hard-rejected with `STALE_POST`. The rejections were correct; paying for them
was not.

`is_human_flagged()` read the Airtable `Prequalification` field and treated
`Send to AI` as a human's reviewed selection. `Prequalification` is a
**formula**. It is computed from `Request Signal`, `Service Signal`, and
`Promotion Signal`, and knows nothing about post age, intent, or human
review. Because the pipeline read it as human judgement, the six records
carrying it skipped the deterministic prefilter entirely and went straight to
the model.

Two rules replace it:

**`Human Decision = Approve` is the only human override.** `Prequalification`
survives as what it always was — a machine eligibility hint that chooses which
records to fetch and breaks ties in the queue order. It overrides nothing.
`Review` is not a decision and changes nothing. `Reject` now stops a record
before the AI call rather than after it.

**A non-overridable gate runs before the AI queue.** `prefilter_post` now
returns machine-readable `rejection_codes`, and any code in
`NON_OVERRIDABLE_HARD_REJECTIONS` — `STALE_POST`, `NO_SERVICE_MATCH`,
`PROMOTIONAL_POST`, `JOB_SEEKER`, `ALREADY_RESOLVED`, `POST_TOO_SHORT` —
rejects the record deterministically, before `get_openai_client()` is called.
Nothing lifts it: not an `Approve`, not the formula, not
`ENFORCE_PYTHON_PREFILTER=false`, which turns off the *heuristic* prefilter
and never covered the age check.

`Approve` now lifts exactly `NO_BUYING_INTENT`, `FUNDING_OR_FINANCE_REQUEST`,
and `HIRING_UNRELATED`, applied by filtering the finished code list against
`HUMAN_OVERRIDABLE_HARD_REJECTIONS`. A rejection code added later is
non-overridable until someone deliberately lists it.

Pre-AI rejections now write their codes to `Disqualifiers`, so a record
rejected before the model says `STALE_POST` in the same column a post-AI
rejection would.

`MAX_POST_AGE_DAYS` is unchanged at 5.

### Fixed — `"working capital"` registered as an API integration lead

The prefilter matched service keywords by substring:

```python
>>> "api" in "working capital"
True
```

An excavation-financing post therefore recorded a BruceTech service match and
was sent to the model. The same flaw affected `pos`, `seo`, `ai`, and every
other short token in the list.

Every service term is now compiled to a word-boundary-anchored regex in
`intent.compile_term`, so `api` matches "integrating an API" and never
"capital". Plural matching is preserved via an optional trailing `s`.

The live Airtable `Service Signal` formula was fixed the same way. Python does
not rely on that fix: formula values calculate asynchronously and cannot be
unit-tested, so both layers enforce the rule independently.

### Fixed — the pipeline could import another Apify run's posts

`/actor-tasks/{id}/runs/last/dataset/items` returns items with no run identity
attached. When a manual Apify run finished between the scheduled scrape and
this read, the pipeline imported that run's posts while believing it had read
its own — and recorded no run ID either way.

`apify_runs.resolve_run` now resolves exactly one **run object** before any
dataset is touched, and reads that run's own `defaultDatasetId`. Three modes:
pinned (`APIFY_RUN_ID`), start-and-poll (`RUN_APIFY_TASK=true`), or resolve the
last successful run as an object. The `runs/last/dataset/items` shortcut is
gone from the codebase and a test asserts it.

The Apify token also moved from a `?token=` query parameter to an
`Authorization` header, so it can no longer appear in a logged URL or an
exception message.

### Added — deterministic intent classification (`intent.py`)

Six types: `PROVIDER_REQUEST`, `IMPLEMENTATION_REQUEST`, `BUSINESS_PAIN`,
`TOOL_RESEARCH`, `GENERAL_ADVICE`, `UNRELATED`.

Intent and service match are independent axes and the prefilter requires
**both**, which is what blocks solution hopping. "Looking for a reliable
virtual assistant" is a genuine provider request that matches no BruceTech
service, so it is rejected without an AI call.

General advice and unrelated posts never reach the model. Tool research may
reach Manual Review but can never produce automatic outreach.

### Added — two-tier service matching

- **Strong terms** (`wordpress`, `crm`, `api`, `gohighlevel`, `pen and paper`)
  establish a match on their own.
- **Weak terms** (`network`, `security`, `payment`, `software`, `system`) need
  corroboration: two distinct weak categories, or one weak term plus technical
  context.

"Our office network keeps dropping" is a managed IT lead. "Trying to grow my
network" and "is a security deposit normal?" are not.

### Added — exact run attribution in Airtable

- One record per Apify run in **Facebook Post Scraper Runs**, upserted on
  `Apify Run ID` so the same run is never logged twice.
- Every imported Raw Signal is written with `Apify Run ID` and a `Scraper Run`
  linked record.
- `Cost`, `Run Input`, `Duration`, and `Dataset URL` are written **only when
  Apify supplies them**. A missing value is omitted from the payload rather
  than written as zero, so a blank cell means "not reported" and any existing
  manual value survives.

### Added — Facebook Group Performance aggregation

Operational counts recomputed from Raw Signals after each run: Posts Scraped,
AI Candidates, System Qualified, Provider Requests, Outreach Ready, Contacted,
Replies, Meetings, Proposals, Won, Last Run.

`Tier`, `Status`, `Revenue`, and `Notes` are human-owned and never written —
enforced by an assertion in `GroupMetrics.to_fields`, not just by convention.

The legacy `Contacted` checkbox is preserved and counted only when
`Outreach Status` is empty, so migrated records are not double-counted.

`Lost` counts as contacted and replied but not as a meeting or proposal: a
deal can be lost at any stage.

### Added — Outreach Ready is now strictly narrower than Qualified

Qualified means strong commercial fit. Outreach Ready additionally requires a
personalised DM, a contactable channel, `classification_confidence` ≥
`MIN_OUTREACH_CONFIDENCE` (0.55), an outreach-eligible intent, and — for
business pain specifically — a score of at least
`BUSINESS_PAIN_OUTREACH_MIN_SCORE` (70).

Business pain is inferred rather than requested, so it carries a higher bar
than someone who explicitly asked to hire.

### Added — six Python-only hard rejection codes

`NO_BUYING_INTENT`, `FUNDING_OR_FINANCE_REQUEST`, `HIRING_UNRELATED`,
`SUPPRESSED_AUTHOR`, `DISALLOWED_GROUP`, `HUMAN_REJECTED`.

These are decided in Python and deliberately excluded from the model's JSON
schema enum. `STALE_POST` was moved to the same category — it was previously
offered to the model despite being computed in Python.

### Added — bounded AI error recovery

A transient failure (timeout, rate limit, 5xx, connection reset) consumes one
attempt and the record is re-queued on a later run. A permanent failure
consumes the whole budget at once. After `AI_ERROR_MAX_ATTEMPTS` the record is
retired from the queue.

The attempt count lives in `AI Output` as JSON, and only on records that
actually failed. A failure never blanks `Lead Score`, `Qualified`, or
`Suggested DM`.

### Added — `Qualification Version`

Every record this release evaluates is stamped `facebook-v2`. Historical
records are never mass-reprocessed and keep their legacy scores, so the two
populations stay comparable.

### Added — Airtable write safety

- `strip_read_only_fields` is applied to every payload before it is sent, so
  formula fields and human-owned fields cannot be written even by mistake.
- `Human Decision` and `Outreach Status` are read and respected, never
  written. A record past `Not Contacted` skips classification entirely.
- A metadata-API preflight fails the run with the missing field names before
  any record is modified. It is skipped with a note when the token lacks the
  `schema.bases:read` scope. No Airtable field is ever created, renamed, or
  deleted.

### Added — `AI Output` is now populated

Previously declared in the schema but never written. It now holds the model's
raw signals plus the Python assessment — intent type, prefilter score and
breakdown, score breakdown. The deterministic intent lives here because
Airtable's `Intent Type` column is a formula and cannot be written to.

### Changed — queue ordering favours intent over recency

New order: human-flagged → imported this run → provider request →
implementation request → Prefilter Score → newest → least comment competition.

Recency previously outranked everything below it, which let a stale backlog of
weak posts consume the whole `AI_BATCH_LIMIT` before the day's real leads were
reached.

### Changed — `MAX_POST_AGE_DAYS` default 45 → 5

The scraper works a roughly 3-day window, and a Facebook request older than a
work week has usually been answered. Override via the workflow env when
deliberately working a backlog.

### Changed — Prefilter Score is documented and intent-driven

A 0–100 prioritisation number with every component published in README.md:
intent base, service match, freshness band, commercial context, length,
comment competition, and named penalties. It gates the AI call and orders the
queue; it is not the Lead Score.

### Changed — anti-solution-hopping instructions to the model

The system prompt now names the failure explicitly, with worked examples for
the virtual-assistant, financing, CRM-research, and GoHighLevel cases, and
forbids inventing business details, urgency, budget, location, or service
requirements.

### Tests

125 → **308 passing**. New files: `tests/test_intent.py` (65),
`tests/test_attribution.py` (24), `tests/test_group_performance.py` (30),
`tests/test_pipeline_v2.py` (64), and `tests/fixtures.py` holding the five
sanitised production fixtures.

All 125 pre-existing tests pass unmodified. No test starts a paid Apify run,
writes to Airtable, or makes an OpenAI request.

### Not verified live

No live Apify, Airtable, or OpenAI call was made from this branch — the
session had no credentials and no network access to those hosts. Everything is
covered by mocked tests against recorded payload shapes. See
`docs/PIPELINE_AUDIT.md` §9 for the full list and the remaining limitations.

---

## [Unreleased] — Deterministic qualification and the 65-point threshold

Branch: `agent/improve-lead-qualification`

This release moves every qualification decision out of the AI model and into
Python. The model now extracts structured signals; Python computes the score,
tier, hard rejections, Qualified flag, and outreach eligibility.

### Changed — qualification threshold raised from 55 to 65

Updated in every location:

- `qualification.DEFAULT_QUALIFICATION_THRESHOLD` (Python default)
- `QUALIFICATION_THRESHOLD` in `.github/workflows/facebook-leads.yml`
- The AI instructions, which no longer reference a threshold at all because
  the model no longer decides qualification
- `README.md`
- The test suite, which asserts the 64 / 65 boundary directly

### Changed — the AI no longer decides qualification

Previously `qualified` was set to `lead_score >= threshold` using a score the
model invented, so a single model judgement drove the outcome and results
were not reproducible.

Now:

- The model returns **signals only**. `qualified` and `lead_score` were
  removed from the JSON schema entirely.
- `qualification.evaluate_lead()` computes the score from weighted signal
  components whose maximums sum to exactly 100.
- Tier, Qualified, and outreach eligibility follow from that score plus the
  hard rejection rules.

### Added — hard rejection rules that override the score

Thirteen conditions, any one of which rejects a post regardless of score:

`PERSONAL_REQUEST`, `PROMOTIONAL_POST`, `COMPETITOR_OR_AGENCY`,
`JOB_SEEKER`, `SPAM_OR_AFFILIATE`, `STUDENT_OR_EDUCATIONAL`,
`FREE_ONLY_REQUEST`, `ALREADY_RESOLVED`, `PROVIDER_ALREADY_SELECTED`,
`NO_BUSINESS_CONTEXT`, `NO_SERVICE_MATCH`, `INAPPROPRIATE_OUTREACH`,
`STALE_POST`.

`STALE_POST` is computed in Python from the post timestamp against
`MAX_POST_AGE_DAYS` and is never taken from the model.

### Added — qualification tiers

| Tier | Score | Qualified |
|---|---|---|
| Hot | ≥ 80 | true |
| Qualified | 65–79 | true |
| Manual Review | 55–64 | **false** |
| Rejected | < 55 or any hard rejection | false |

Only Hot and Qualified may set `Qualified = true`. Manual Review records get
`Qualified = false`, no DM, `Manual Review = true`, and a `[MANUAL REVIEW]`
prefix on the lead summary.

### Removed — the generic fallback DM

The old pipeline synthesised a template message ("I saw your post and thought
BruceTech may be able to help with…") whenever a threshold-qualified record
had no AI-written DM, purely to keep it visible in the Outreach Ready view.

That fallback is gone. A lead without enough evidence for a specific message
is qualified but **not** outreach-ready. The AI instructions now state
explicitly that a blank DM is correct and expected.

### Changed — `do_not_contact` is no longer overridden

The old code rewrote `recommended_channel` from `do_not_contact` to
`direct_message` for any qualified record, discarding the model's judgement
that outreach was inappropriate. The recommendation is now respected as-is.

### Added — 19 structured AI signals

`intent_strength`, `business_context`, `buyer_role`, `service_categories`,
`problem_specificity`, `purchase_signal`, `urgency`, `business_impact`,
`location`, `resolved_status`, `provider_already_selected`,
`personal_request`, `free_only_request`, `promotional_post`,
`competitor_or_agency`, `spam_risk`, `outreach_appropriateness`,
`classification_confidence`, `disqualifier_codes`.

The schema is `strict` with `additionalProperties: false`.

### Added — deterministic Python prefilter

`qualification.prefilter_post()` screens posts on intent keywords, service
keywords, negative keywords, text length, and post age before any AI call, so
obvious rejects cost no tokens.

The undocumented Airtable `Prequalification` formula is no longer required.
`REQUIRE_AIRTABLE_PREQUALIFICATION` (default `false`) can re-enable it as an
additional gate.

### Added — URL normalisation and stronger deduplication

New `normalization.py`:

- Canonical Facebook URLs: scheme and host normalised
  (`m.`/`web.`/`mbasic.`/`touch.` → `www.facebook.com`), tracking parameters
  removed (`fbclid`, `__cft__[n]`, `__tn__`, `mibextid`, `utm_*`, and
  others), trailing slash stripped, remaining parameters sorted.
- Post ID extraction from `legacyId` or from `/posts/`, `/permalink/`,
  `/groups/…/posts/`, `/videos/`, `/reel/`, and `story_fbid` URL shapes.
- SHA-256 text fingerprints that ignore case, punctuation, and whitespace.
- Deduplication on post ID, then canonical URL, then author-scoped
  fingerprint.

Previously Airtable deduplicated on the raw URL string, so the same post with
different tracking parameters or a different Facebook host was imported more
than once.

### Added — AI queue prioritisation

The queue is now ordered by: records imported in the current run, then newest
post, then strongest prefilter score, then highest buying-intent signal. The
pipeline fetches more candidates than `AI_BATCH_LIMIT` and applies the limit
after sorting, so the batch is spent on the best candidates.

### Added — optional fresh Apify run mode

With `APIFY_START_NEW_RUN=true` the pipeline starts a new task run, polls
until it reaches a terminal state, and reads that exact run's dataset. This
removes the race where `runs/last` returned a previous run's data. Bounded by
`APIFY_RUN_TIMEOUT_SECONDS` and `APIFY_RUN_POLL_SECONDS`, and exposed as a
checkbox on manual workflow dispatch.

### Added — GitHub Actions concurrency protection

A `facebook-leads-pipeline` concurrency group prevents overlapping runs.
`cancel-in-progress` is `false` so an in-flight run finishes its Airtable
writes instead of being killed mid-batch.

A `test` job (compile + pytest) now runs before `run-pipeline`, so the
pipeline only executes when the suite passes.

### Changed — lazy initialisation

- The OpenAI client is created on first use via `get_openai_client()` instead
  of at import.
- All environment variables are read through `env_str` / `env_int` /
  `env_bool` with defaults, and secrets are validated by `require_env()` at
  the point of use.

Previously the module raised `KeyError` at import if any of six variables was
missing, which made the qualification rules impossible to test.

### Added — regression test suite

116 tests across `tests/`, requiring no credentials or network:

- score 64 does not qualify; score 65 does
- hard rejection overrides a score of 100
- resolved, promotional, personal, and stale posts are rejected
- every one of the 13 hard rejection conditions
- manual review has no DM and `Qualified = false`
- no fallback DM is ever generated
- `do_not_contact` is never upgraded
- Facebook URL normalisation across 12 variants, and duplicate detection
- queue prioritisation ordering
- schema completeness and lazy initialisation

### Added — Airtable fields

`Lead Tier`, `Manual Review`, `Outreach Ready`, `Disqualifiers`,
`Prefilter Score`. These must be created in Airtable; see README.md. If they
are absent the pipeline detects `UNKNOWN_FIELD_NAME` on its first write,
warns once, and continues with the core fields.

### Added — documentation

- `README.md`: architecture, qualification rules, thresholds, environment
  variables, Airtable fields, local runs, tests, deployment.
- `CHANGELOG.md`: this file.
- `docs/PUSH_STATUS.md`: branch, files changed, validation, test results.
- `.gitignore`: excludes `__pycache__`, `.pytest_cache`, `.env`.
  A committed `.pyc` file was removed from version control.
