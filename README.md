# BruceTech Facebook Lead Pipeline

Scrapes public Facebook group posts via Apify, deduplicates and imports them
into Airtable, then qualifies them for BruceTech outreach.

The defining rule of this pipeline: **the AI does not decide who is a lead.**
It extracts structured observations about a post. Python applies the scoring
and qualification rules deterministically, so the same post always produces
the same decision and every rule is unit-testable.

---

## Architecture

```
Apify task run
      │
      ▼
Deduplication ─── post ID · canonical URL · text fingerprint · author
      │
      ▼
Airtable import (new posts only)
      │
      ▼
Deterministic prefilter ─── intent + service keywords, length, post age
      │                     (obvious rejects never reach the AI)
      ▼
Prioritisation ─── Send to AI › this run › newest › prefilter › intent
      │
      ▼
AI signal extraction ─── 21 structured signals, no score, no verdict
      │
      ▼
Deterministic qualification (Python)
      │  score → tier → hard rejections → Qualified → outreach eligibility
      ▼
Airtable write-back
```

### Files

| File | Purpose |
|---|---|
| `run_pipeline.py` | Orchestration, Apify, Airtable, OpenAI, prioritisation |
| `qualification.py` | Scoring, tiers, hard rejections, prefilter. Pure logic |
| `normalization.py` | URL canonicalisation, fingerprinting, deduplication |
| `tests/` | Regression suite (no credentials required) |
| `docs/APIFY_SCRAPERS.md` | Which Apify sources fit this pipeline, and why |

`qualification.py` and `normalization.py` deliberately have no environment,
network, or third-party dependencies, so they can be imported and tested in
isolation.

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

In **website analysis focus mode** one bounded bonus is added before the same
clamp — see [Website analysis focus](#website-analysis-focus).

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
| `NO_WEBSITE_OPPORTUNITY` | Focus mode only: the post describes no website need |

`STALE_POST` is computed in Python from the post timestamp and is never
trusted from the model. `NO_WEBSITE_OPPORTUNITY` is derived in Python too and
is deliberately not offered to the model, so it can never appear in a run
that is not in website analysis focus mode.

### Outreach eligibility

A lead is outreach-ready only when **all** of the following hold:

1. Tier is Hot or Qualified
2. The AI wrote a specific, personalised DM
3. `recommended_channel` is not `do_not_contact`

There is **no fallback DM**. If the AI cannot write a specific message, the
record stays qualified but is not outreach-ready. A `do_not_contact`
recommendation is respected and never upgraded to `direct_message`.

---

## The AI queue

Each run fetches candidates in two phases:

1. **Every** record where `Prequalification = Send to AI` and `AI Status` is
   blank or `Pending`.
2. The newest remaining unprocessed records, sorted server-side by post
   time, to fill the window (`AI_BATCH_LIMIT × 5`).

Phase 1 exists so a curated selection is never lost behind an arbitrary page
of the backlog. Phase 2 is sorted because truncating an unsorted Airtable
query returns records in table order, not by relevance.

### `Send to AI` overrides the prefilter

A record you flag `Send to AI` sorts above everything else — including
records imported in the current run — and **bypasses the keyword prefilter
entirely**. If you have reviewed a record and marked it, it goes to the AI
even if the keyword heuristics would have rejected it.

Every other record that fails the prefilter is marked
`AI Status = Processed`, `Lead Tier = Rejected` **without an AI call**, and
is not evaluated again. That is intended behaviour: it keeps the backlog
from growing and keeps token spend on plausible leads. It also means a
borderline post can be rejected on keywords alone — flag it `Send to AI` if
you want the model to decide instead.

Set `ENFORCE_PYTHON_PREFILTER=false` to send everything to the AI.

---

## Website analysis focus

Tick **Website analysis** on **Run workflow** (or set
`WEBSITE_FOCUS_MODE=true`) to point the whole run at website work for
business owners and business pages. It is off by default; nothing below
happens in a normal run.

The five offers it hunts for, strongest first:

| Offer | `website_opportunity` | Bonus |
|---|---|---:|
| Paying a monthly platform fee they resent (Shopify, Wix, Squarespace) — rebuild on WordPress and kill the recurring cost | `expensive_platform` | 12 |
| Wants off their current platform | `platform_migration` | 11 |
| Has customers but no website — often trading from a Facebook page alone | `no_website` | 11 |
| Wants a site built | `new_website_build` | 10 |
| Site is down, slow, erroring, or not mobile friendly | `broken_or_failing_website` | 9 |
| Old, dated, or abandoned site | `outdated_website` | 8 |
| Wants the existing site redesigned | `redesign_or_refresh` | 8 |
| Wants to sell online | `ecommerce_store` | 7 |
| Anything else website-shaped | `other_website_need`, `maintenance_or_updates`, `seo_or_visibility` | 5 · 5 · 4 |

A further **+4** applies when `website_platform` is one of Shopify, Wix,
Squarespace, BigCommerce, or Webflow — the business is already paying a
monthly plan, so the conversion pitch is a number rather than an opinion.
WordPress and WooCommerce get no bonus; there is no plan to replace.

The combined bonus is capped at **14** and the total is still clamped to 100,
so the 0–100 scale and every threshold are unchanged.

Focus mode changes exactly three things:

1. **The prefilter narrows.** Only website keywords count as a service match,
   so managed IT, Microsoft 365, networking, and automation posts are
   screened out before the AI. A website-opportunity phrase ("still paying
   Shopify every month", "we only have a Facebook page") counts as buying
   intent on its own, so a business describing its situation without asking
   for anything still gets through.
2. **Non-website posts are hard-rejected** as `NO_WEBSITE_OPPORTUNITY`,
   however strong a lead they would otherwise be. A 100-point managed-IT lead
   is Rejected in this mode.
3. **Website leads sort first** in the queue, ahead of the general prefilter
   score, so the batch is spent on them.

Everything else is untouched: the model still decides nothing, hard
rejections still beat any score, and `Send to AI` still overrides the
prefilter.

`website_opportunity` and `website_platform` are extracted and written to
Airtable on **every** run, focus or not, so you can build a view of website
opportunities from existing data before you ever tick the box.

### Suggested use

Run it as a themed pass rather than the default: tick **Website analysis**
and **Dry run** together first to see what the current backlog holds, then
run it for real with a small **AI batch limit**. Leave the box unticked for
normal full-service runs.

---

## Environment variables

Secrets — set as GitHub Actions secrets, never committed:

| Variable | Purpose |
|---|---|
| `APIFY_TOKEN` | Apify API token |
| `APIFY_TASK_ID` | Apify task to read or run |
| `AIRTABLE_TOKEN` | Airtable personal access token |
| `AIRTABLE_BASE_ID` | Airtable base ID |
| `AIRTABLE_TABLE_NAME` | Airtable table name |
| `OPENAI_API_KEY` | OpenAI API key |

Configuration — safe to set in the workflow:

| Variable | Default | Purpose |
|---|---|---|
| `QUALIFICATION_THRESHOLD` | `65` | Minimum score to qualify |
| `MANUAL_REVIEW_THRESHOLD` | `55` | Minimum score for manual review |
| `HOT_LEAD_THRESHOLD` | `80` | Minimum score for the Hot tier |
| `MAX_POST_AGE_DAYS` | `45` | Posts older than this are rejected |
| `OPENAI_MODEL` | `gpt-5-mini` | Model used for signal extraction |
| `OPENAI_MAX_OUTPUT_TOKENS` | `2500` | Output token ceiling |
| `OPENAI_MAX_ATTEMPTS` | `3` | Retries per post |
| `OPENAI_RETRY_DELAY_SECONDS` | `5` | Base retry delay |
| `AI_BATCH_LIMIT` | `20` | Max AI calls per run |
| `MAX_POST_CHARS` | `8000` | Post text truncation |
| `AIRTABLE_FORMULA_WAIT_SECONDS` | `15` | Pause after import |
| `DRY_RUN` | `false` | Read and score, but make no Airtable writes |
| `ENFORCE_PYTHON_PREFILTER` | `true` | Skip the AI for obvious rejects |
| `WEBSITE_FOCUS_MODE` | `false` | Score website work only; reject everything else |
| `REQUIRE_AIRTABLE_PREQUALIFICATION` | `false` | Also require the Airtable formula |
| `APIFY_START_NEW_RUN` | `false` | Start a fresh Apify run and await it |
| `APIFY_RUN_TIMEOUT_SECONDS` | `900` | Max wait for a fresh run |
| `APIFY_RUN_POLL_SECONDS` | `15` | Poll interval while waiting |

All are read lazily. Importing `run_pipeline` requires no credentials, which
is what lets the test suite run in CI without secrets.

---

## Airtable fields

Existing fields, unchanged:

`Url` · `Facebook url` · `Time` · `User ID` · `User name` · `Text` ·
`Group title` · `Input Url` · `Likes count` · `Comments count` ·
`Shares count` · `Prequalification` · `AI Status` · `Qualified` ·
`Lead Score` · `Service Match` · `Lead Summary` · `Rejection Reason` ·
`Suggested DM` · `Recommended channel` · `Evidence`

**Added fields — create these before deploying:**

| Field | Type | Purpose |
|---|---|---|
| `Lead Tier` | Single line text | Hot / Qualified / Manual Review / Rejected |
| `Manual Review` | Checkbox | Needs a human decision |
| `Outreach Ready` | Checkbox | Qualified **and** has a personalised DM |
| `Disqualifiers` | Long text | Comma-separated hard rejection codes |
| `Prefilter Score` | Number | Deterministic pre-AI score |
| `Website Opportunity` | Single line text | Kind of website work the post points to |
| `Website Platform` | Single line text | Platform the business is on today |

If these fields do not exist, the pipeline detects the `UNKNOWN_FIELD_NAME`
error on its first write, prints a warning, and continues writing the core
fields only. Tiering and manual-review views will not work until they are
created.

> **Update your Airtable views.** An "Outreach Ready" view should now filter
> on `Outreach Ready = checked` rather than on `Qualified` alone, because
> qualified leads without a personalised DM are intentionally not
> outreach-ready.

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
last completed one:

```bash
APIFY_START_NEW_RUN=true python run_pipeline.py
```

Never commit real credentials. Use environment variables or a local `.env`
file that is git-ignored.

---

## Testing against your real Airtable data

`DRY_RUN=true` reads Apify and Airtable and calls the AI exactly as normal,
but makes **no Airtable writes**. It prints the decision it would have
written for each record:

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

The suite needs no API keys, no network, and no Airtable base. It covers:

- the 65-point threshold, including the 64 / 65 boundary
- every hard rejection condition
- hard rejections overriding a perfect score
- tier boundaries and the Qualified-only-for-Hot/Qualified rule
- manual-review records having no DM and `Qualified = false`
- the absence of any fallback DM
- `do_not_contact` never being upgraded
- URL normalisation across host, scheme, tracking, and slash variants
- duplicate detection by post ID, canonical URL, and text fingerprint
- queue prioritisation ordering
- website analysis focus: the `NO_WEBSITE_OPPORTUNITY` rejection, the capped
  bonus, the narrowed prefilter, and the guarantee that a normal run is
  byte-for-byte unaffected

---

## Deployment

The pipeline runs from GitHub Actions
(`.github/workflows/facebook-leads.yml`).

1. Add the six secrets under **Settings → Secrets and variables → Actions**.
2. Create the seven added Airtable fields listed above.
3. Merge to `main`.

> **The daily schedule is currently paused.** The `schedule:` block in
> `.github/workflows/facebook-leads.yml` is commented out while the new
> qualification rules are validated against the production base. Uncomment
> the three lines to restore the 06:30 America/Toronto run.

Until then the pipeline runs only via **Run workflow**, which offers four
inputs: **Dry run** (score without writing), **Website analysis** (score
website work only), **Start new Apify run**, and **AI batch limit**.

Two jobs run in sequence: `test` (compile + pytest) then `run-pipeline`. The
pipeline only runs if the tests pass.

A `concurrency` group named `facebook-leads-pipeline` guarantees two runs
never overlap. `cancel-in-progress` is deliberately `false` so an in-flight
run is allowed to finish its Airtable writes rather than being killed
mid-batch.
