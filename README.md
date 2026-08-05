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
Prioritisation ─── this run › newest › prefilter score › buying intent
      │
      ▼
AI signal extraction ─── 19 structured signals, no score, no verdict
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

`STALE_POST` is computed in Python from the post timestamp and is never
trusted from the model.

### Outreach eligibility

A lead is outreach-ready only when **all** of the following hold:

1. Tier is Hot or Qualified
2. The AI wrote a specific, personalised DM
3. `recommended_channel` is not `do_not_contact`

There is **no fallback DM**. If the AI cannot write a specific message, the
record stays qualified but is not outreach-ready. A `do_not_contact`
recommendation is respected and never upgraded to `direct_message`.

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
| `ENFORCE_PYTHON_PREFILTER` | `true` | Skip the AI for obvious rejects |
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

**New fields — create these before deploying:**

| Field | Type | Purpose |
|---|---|---|
| `Lead Tier` | Single line text | Hot / Qualified / Manual Review / Rejected |
| `Manual Review` | Checkbox | Needs a human decision |
| `Outreach Ready` | Checkbox | Qualified **and** has a personalised DM |
| `Disqualifiers` | Long text | Comma-separated hard rejection codes |
| `Prefilter Score` | Number | Deterministic pre-AI score |

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

---

## Deployment

The pipeline runs from GitHub Actions
(`.github/workflows/facebook-leads.yml`).

1. Add the six secrets under **Settings → Secrets and variables → Actions**.
2. Create the five new Airtable fields listed above.
3. Merge to `main`.

The workflow runs daily at 06:30 America/Toronto and can be triggered
manually with **Run workflow**, which offers a checkbox to start a fresh
Apify run.

Two jobs run in sequence: `test` (compile + pytest) then `run-pipeline`. The
pipeline only runs if the tests pass.

A `concurrency` group named `facebook-leads-pipeline` guarantees two runs
never overlap. `cancel-in-progress` is deliberately `false` so an in-flight
run is allowed to finish its Airtable writes rather than being killed
mid-batch.
