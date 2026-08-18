# Pipeline Audit — intent, run attribution, and qualification precision

Branch: `agent/facebook-pipeline-v2-intent-attribution`
Qualification version: `facebook-v2`

This document records what the pipeline looked like before this release, what
was wrong with it, and what changed. It exists so the next person to touch
this code does not have to re-derive the reasoning from the diff.

---

## 1. What was already implemented

A previous release had already moved qualification out of the model and into
Python. That work was correct and has been **preserved in full**, not rewritten:

| Capability | Where | Status |
|---|---|---|
| Qualification threshold of 65 | `qualification.DEFAULT_QUALIFICATION_THRESHOLD` | Preserved |
| Deterministic scoring, components summing to exactly 100 | `qualification.score_signals` | Preserved |
| 13 hard rejection codes overriding the score | `qualification.collect_hard_rejections` | Extended |
| Lead tiers (Hot / Qualified / Manual Review / Rejected) | `qualification.resolve_tier` | Preserved |
| URL canonicalisation across hosts and tracking params | `normalization.normalize_facebook_url` | Preserved |
| Text-fingerprint deduplication, author-scoped | `normalization.text_fingerprint` | Preserved |
| Stale-post rejection computed in Python | `qualification.collect_hard_rejections` | Preserved |
| Lazy OpenAI client construction | `run_pipeline.get_openai_client` | Preserved |
| Structured AI signal extraction, no score from the model | `run_pipeline.LEAD_SCHEMA` | Extended |
| No generic fallback DM | `qualification.evaluate_lead` | Preserved |
| `do_not_contact` never upgraded | `qualification.evaluate_lead` | Preserved |
| GitHub Actions concurrency group | `.github/workflows/facebook-leads.yml` | Preserved |
| Optional Apify task execution | `run_pipeline.collect_apify_posts` | Rebuilt (see §7) |
| Regression suite, 125 tests, no credentials required | `tests/` | Preserved and extended |

The 125 pre-existing tests still pass unmodified. Nothing in this release
changes a documented scoring weight.

---

## 2. What was missing

| Gap | Consequence |
|---|---|
| No intent classification anywhere | A post's *shape* was invisible. "How do I get financing" and "who can rebuild my site" were treated identically if their keywords overlapped. |
| Substring service matching | `"api" in "working capital"` → an excavation-financing post scored as an API integration lead. See §5. |
| No exact Apify run attribution | Imported posts could come from a different run than the pipeline believed, and no run ID was ever recorded. See §7. |
| No scraper run logging | Cost per lead and cost per group were unanswerable. |
| No Raw Signal → Scraper Run link | Impossible to trace a customer back to the scrape that found them. |
| No group performance aggregation | No measurement loop from group to won deal. |
| No Qualification Version | Legacy and new qualification results were indistinguishable in the same table. |
| Human Decision and Outreach Status not respected | A classification run could overwrite a recorded sales outcome. |
| AI errors permanently excluded a record | One transient timeout removed a lead from the queue forever. |
| `MAX_POST_AGE_DAYS` default of 45 | Six-week-old requests were still being scored and contacted. |

---

## 3. What changed

### New modules

| File | Purpose | Lines |
|---|---|---|
| `intent.py` | Word-boundary service matching, intent classification, negative-signal detection | ~560 |
| `apify_runs.py` | Exact run resolution, polling, dataset retrieval | ~430 |
| `scraper_runs.py` | Idempotent Airtable run logging keyed by Apify Run ID | ~250 |
| `group_performance.py` | Per-group operational metrics from Raw Signals | ~400 |

### Modified

| File | Change |
|---|---|
| `qualification.py` | Prefilter rebuilt on `intent`; six new Python-only rejection codes; intent-gated outreach; confidence floor; business-pain score bar; `MAX_POST_AGE_DAYS` 45 → 5 |
| `run_pipeline.py` | Exact-run wiring; run attribution on import; read-only field guard; bounded cross-run AI retry; Qualification Version; intent-aware queue ordering; run summary; schema preflight |
| `.github/workflows/facebook-leads.yml` | New configuration; `MAX_POST_AGE_DAYS: 5`; `apify_run_id` input. Schedule and concurrency unchanged |

### New tests

`tests/test_intent.py` (65), `tests/test_attribution.py` (24),
`tests/test_group_performance.py` (30), `tests/test_pipeline_v2.py` (64),
plus `tests/fixtures.py`. Total suite: **308 passing**.

---

## 4. Historical false-positive examples

These are the post shapes the old pipeline scored as leads. All five are now
rejected before an OpenAI call, or blocked from outreach.

| Post shape | Old behaviour | New behaviour |
|---|---|---|
| "I own an excavation company and need business funding … and working capital." | Service match via `api` ⊂ `capital`; sent to the model | `GENERAL_ADVICE`, no service match, `FUNDING_OR_FINANCE_REQUEST` |
| "Looking for a reliable virtual assistant." | Provider request → treated as an automation lead | `UNRELATED`, no service match, never reaches the model |
| "What CRM are other contractors using?" | Service match on `crm` → qualified and DM'd | `TOOL_RESEARCH`, may reach Manual Review, **never** auto-outreach |
| "Should I hire an employee or stay solo?" | Weak keyword hits | `GENERAL_ADVICE`, `HIRING_UNRELATED` |
| "Does anyone know if a security deposit is normal for a commercial lease?" | `security` matched cybersecurity | Weak term with no corroboration → no service match |
| "Trying to grow my network in the trades." | `network` matched managed IT | Weak term, no technical context → no service match |

---

## 5. The `api` / `capital` regression, in full

**Cause.** `qualification.SERVICE_KEYWORDS` was a tuple of plain strings
matched with Python's `in` operator:

```python
service_hits = [word for word in SERVICE_KEYWORDS if word in lowered]
```

`"api"` was in that tuple. `"capital"` contains the letters `a-p-i` at index 1.
So:

```python
>>> "api" in "i own an excavation company and need … working capital"
True
```

The post registered a service match, passed the service half of the prefilter,
and was sent to the model as an integration lead.

The same bug affected every short token in the list — `pos`, `seo`, `ai`, and
by extension common words like `security`, `network`, and `payment` that match
legitimately but in unrelated contexts.

**Fix.** `intent.compile_term` compiles every term to a `\b`-anchored regex:

```python
re.compile(rf"\b{escaped}(?:s)?\b", re.IGNORECASE)
```

`\bapi\b` matches "integrating an API" and cannot match inside "capital".
The optional trailing `s` preserves plural matching ("backups", "integrations").

**Guarded by.** `test_working_capital_does_not_match_api`,
`test_api_token_still_matches_when_it_is_a_real_word`,
`test_capital_never_matches_api` (4 cases),
`test_short_tokens_do_not_match_inside_words` (6 cases).

The live Airtable `Service Signal` formula was fixed the same way. Python does
not depend on that fix — formula values calculate asynchronously and cannot be
unit-tested, so both layers enforce the rule independently.

---

## 6. Previous prequalification weakness

The old Python prefilter required *one intent keyword* and *one service
keyword*, both by substring. That is a much weaker rule than the corrected
Airtable formula, which requires:

```
Request Signal = 1  AND  Service Signal = 1  AND  Promotion Signal = 0
```

Three specific weaknesses:

1. **No promotion check.** The old prefilter had a `NEGATIVE_KEYWORDS`
   penalty, but a promotional post with enough intent and service hits could
   still clear the 30-point bar.
2. **Intent keywords were shape-blind.** `"need a"`, `"looking to"`, and
   `"how much"` matched a financing question as readily as a hiring request.
3. **Substring service matching**, as above.

The new prefilter mirrors the formula's structure and adds intent:

```
service_matched
  AND intent ∈ {PROVIDER_REQUEST, IMPLEMENTATION_REQUEST, BUSINESS_PAIN, TOOL_RESEARCH*}
  AND NOT promotional
  AND NOT job_seeking
  AND NOT resolved
  AND prefilter_score >= PREFILTER_PASS_SCORE
```

\* Tool research passes only when `ALLOW_TOOL_RESEARCH_TO_AI` is true, and can
never become outreach-ready regardless.

---

## 7. The Apify run-attribution problem

**Cause.** The pipeline read:

```
GET /v2/actor-tasks/{taskId}/runs/last/dataset/items?status=SUCCEEDED
```

That endpoint returns *items*. It never tells you which run produced them.

Two consequences:

1. **Wrong-run imports.** Scheduled GitHub runs and manual Apify runs coexist.
   If a manual run finished between the scheduled scrape and this read, the
   pipeline imported the manual run's posts while believing it had read its
   own. Nothing detected it.
2. **No attribution.** Even when the right posts arrived, no run ID was
   recorded, so "which scrape found this customer, and what did it cost?" had
   no answer.

`APIFY_START_NEW_RUN=true` partly addressed (1) by starting a run and polling
it, but that path was off by default and still logged nothing.

**Fix.** `apify_runs.resolve_run` resolves exactly one **run object** before
any dataset is touched, in three modes:

| Mode | Trigger | Behaviour |
|---|---|---|
| Pinned | `APIFY_RUN_ID` set | Fetch that exact run; wait if still active; fail if it did not succeed |
| Start | `RUN_APIFY_TASK=true` | `POST …/runs`, then poll `/actor-runs/{thatRunId}` to a terminal state |
| Resolve | default | `GET …/runs/last?status=SUCCEEDED` — returns the **run object**, not items |

In every mode the run's own `defaultDatasetId` is what gets read. The
`runs/last/dataset/items` shortcut no longer appears anywhere in the codebase,
and a test asserts that.

The token also moved from a `?token=` query parameter to an `Authorization`
header, so it can never appear in a logged URL or an exception message.

Terminal states handled explicitly: `SUCCEEDED`, `FAILED`, `ABORTED`,
`TIMED-OUT`. `ApifyRunFailed` and `ApifyRunNotFinished` are distinct exceptions
so a broken scrape is distinguishable from a slow one.

---

## 8. Updated architecture

```
Apify task
      │
      ▼
resolve ONE exact run ──────────────► Facebook Post Scraper Runs
  id · dataset · duration · cost           (upsert by Apify Run ID)
      │                                          │
      ▼                                          │
that run's dataset only                          │
      │                                          │
      ▼                                          │
dedupe: post ID · canonical URL · fingerprint    │
      │                                          │
      ▼                                          ▼
Airtable import ─────── Apify Run ID + Scraper Run link
      │
      ▼
deterministic intent ─── PROVIDER · IMPLEMENTATION · PAIN
      │                  TOOL_RESEARCH · GENERAL_ADVICE · UNRELATED
      ▼
word-boundary service match ─── strong · weak+context · none
      │
      ▼
Prefilter Score ─── gate the AI call, order the queue
      │
      ▼   (general advice and unrelated posts stop here, free)
      │
prioritised AI queue ─── human-flagged › this run › provider ›
      │                  implementation › score › fresh › quiet
      ▼
AI signal extraction ─── 24 structured signals, no score, no verdict
      │
      ▼
deterministic scoring (Python) ─── 0-100 · tier · hard rejects
      │
      ▼
Qualified ──────────► strong commercial fit
      │
      ▼
Outreach Ready ─────► qualified AND appropriate AND evidenced
      │               AND confident AND right intent
      ▼
Airtable write-back ─── + Qualification Version
      │
      ▼
Facebook Group Performance ─── scraped → candidates → qualified →
                               outreach → contacted → replied →
                               meeting → proposal → won
```

---

## 9. Remaining limitations

Honest list. None of these block the release; all are recorded so they are not
rediscovered as surprises.

### Not verified against live services

**No live Apify, Airtable, or OpenAI call was made from this branch.** The
session had no credentials and no network access to those hosts. Everything
below is covered by mocked tests against recorded payload shapes, not by a
production round trip:

- Apify run resolution, polling, and dataset retrieval
- The `usageTotalUsd` and `stats.runTimeSecs` fields being present in practice
  for this account's token (they are documented, but a token that does not own
  the run does not receive `usageTotalUsd`)
- Airtable linked-record writes to `Scraper Run`
- The scraper-run upsert against the real `Facebook Post Scraper Runs` table
- Group performance writes
- Real model output against the extended instructions

A manual `workflow_dispatch` run with **Dry run** ticked and a low
`ai_batch_limit` is the recommended first step.

### AI attempt counting uses `AI Output`

The live schema has no dedicated error or attempt-count field, and this task
does not add Airtable fields. The cross-run attempt counter is therefore stored
as JSON in `AI Output`, and only on records that actually failed.

Limitation: if `AI Output` holds anything that is not this JSON shape — legacy
text, or real model output from a previous success — the count reads as zero
and the record gets a fresh budget of `AI_ERROR_MAX_ATTEMPTS`. In the worst
case a record retries three times per "generation" of output rather than three
times ever. It cannot loop infinitely, and a dedicated `AI Attempts` number
field would remove the caveat entirely.

### Deterministic intent is not written to a queryable column

`Intent Type`, `Provider Intent Signal`, and `Research Intent Signal` are
Airtable **formula** fields and cannot be written to. The Python classification
is therefore recorded inside the `AI Output` JSON blob, which is readable but
not filterable in a view.

Group performance recomputes intent from post text rather than reading the
formula, so the counts do not depend on formula timing — but a human filtering
the table by `Intent Type` is seeing Airtable's formula, which may disagree
with Python at the margins. A writable single-select `Python Intent` field
would close this gap.

### `AI Candidates` is inferred from `Evidence`

`Evidence` is only ever written from model output, so a non-blank value
reliably marks a record that cost a token. Records processed by the legacy
pipeline may not have it, so historical `AI Candidates` counts can understate.
Counts from `facebook-v2` onward are exact.

### Funnel inference from a single-select

`Outreach Status` records the *current* state, so earlier stages are inferred
from ordering. `Lost` is deliberately counted as contacted and replied but
**not** as a meeting or proposal, because a deal can be lost at any stage.
`Meetings` and `Proposals` therefore slightly understate rather than inflate.

### Group performance reads the whole Raw Signals table

One paged read per pipeline run. Cheap next to the AI calls today, and exact
rather than incremental, but it grows linearly with the table. If Raw Signals
passes roughly 50k rows this should become an incremental or view-scoped read.

### Intent classification is regex-based

It handles the shapes in `tests/fixtures.py` and the patterns observed in
production, but it is pattern matching, not comprehension. Novel phrasings will
land in `UNRELATED` and be rejected without an AI call. That is the intended
failure direction — a missed lead costs less than a bad DM — but it means
`Send to AI` remains the escape hatch, and it still bypasses the prefilter
entirely.

### `MAX_POST_AGE_DAYS` dropped from 45 to 5

This is a large behavioural change and will visibly reduce volume. It matches
the ~3-day scraper window. Raise it temporarily via the workflow env when
deliberately working a backlog.

### Expect fewer leads

Between the intent veto, strict service matching, the confidence floor, the
business-pain score bar, and the age change, this release rejects
substantially more than its predecessor. That is the goal — fewer, better
leads — but it is a visible drop, and the comparison is only meaningful
against records carrying `Qualification Version = facebook-v2`.
