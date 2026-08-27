# BruceTech Facebook Lead Pipeline

[![Tests](https://github.com/brucetechnicalservices-collab/BT-facebook-lead-pipeline/actions/workflows/tests.yml/badge.svg)](https://github.com/brucetechnicalservices-collab/BT-facebook-lead-pipeline/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/Python-3.12-blue)
![Status](https://img.shields.io/badge/status-active-success)

A production-oriented lead qualification pipeline that turns public Facebook group posts into structured, scored, and reviewable sales opportunities.

The system uses **Apify** for collection, **Airtable** for operational storage and CRM workflow, **OpenAI** for structured signal extraction, and **deterministic Python rules** for final lead scoring and qualification.

> **Key design principle:** the AI does not decide who is a lead. It extracts structured observations. Python applies the qualification rules, thresholds, and hard rejections so decisions are reproducible and testable.

---

## Overview

The pipeline was built to solve a practical lead-generation problem:

1. Scrape public Facebook group posts.
2. Import only new records.
3. Identify whether a post contains relevant business intent.
4. Avoid wasting AI calls on obvious non-leads.
5. Extract structured sales signals with an LLM.
6. Score and qualify leads deterministically.
7. Write decisions, evidence, and suggested outreach back to Airtable.
8. Track group-level funnel performance over time.

The system is designed to favor **high-intent, service-relevant opportunities** over raw lead volume.

---

## Architecture

```text
Facebook Groups
      |
      v
    Apify
      |
      v
Resolve exact scraper run
      |
      v
Read that run's dataset
      |
      v
Normalize + deduplicate
      |
      v
    Airtable
      |
      v
Deterministic intent classification
      |
      v
Service-fit matching
      |
      v
Pre-AI rejection + prioritization
      |
      v
Prioritized AI queue
      |
      v
OpenAI structured signal extraction
      |
      v
Deterministic Python qualification
      |
      +--------------------+
      |                    |
      v                    v
Lead decision         Suggested outreach
      |                    |
      +----------+---------+
                 |
                 v
           Airtable CRM
                 |
                 v
      Group performance metrics
```

### Core separation of responsibilities

| Layer | Responsibility |
|---|---|
| **Apify** | Collect public Facebook group posts |
| **Airtable** | Operational datastore, review workflow, lead state |
| **Python** | Intent classification, service matching, scoring, qualification, safety rules |
| **OpenAI** | Structured signal extraction and personalized outreach copy |
| **GitHub Actions** | CI, manual execution, and deployment workflow |

---

## Engineering Highlights

### Deterministic qualification

The model returns structured signals such as:

- intent strength
- business context
- service fit
- urgency
- purchase signal
- business impact
- buyer role
- outreach appropriateness
- classification confidence

Python converts those signals into a reproducible lead score and tier.

```text
Hot           >= 80
Qualified     65-79
Manual Review 55-64
Rejected      < 55 or hard rejection
```

Hard rejection rules can override the score, preventing a high numerical score from qualifying a post that is stale, promotional, unrelated, already resolved, or otherwise unsuitable.

### Intent classification before AI

Every post is classified before an AI request is made:

- Provider Request
- Implementation Request
- Business Pain
- Tool Research
- General Advice
- Unrelated

Intent and service fit are separate checks. A post can contain real buying intent and still be rejected if it does not map to a service the business provides.

### Service matching without substring false positives

Service terms use word-boundary-aware matching rather than naive substring checks.

```python
"api" in "working capital"                  # unsafe substring match
compile_term("api").search("working capital")  # no match
```

Strong and weak service terms are handled differently so ordinary business language does not automatically become a technical lead.

### AI is used selectively

The deterministic prefilter runs before OpenAI.

Obvious non-leads are rejected without consuming an AI request. Eligible records are then ordered by factors such as:

- explicit human approval
- current-run membership
- provider or implementation intent
- prefilter score
- freshness
- comment competition

This keeps model usage focused on the records most likely to matter.

---

## Reliability and Safety

The pipeline includes production safeguards beyond basic lead scoring.

### Exact scraper-run attribution

Each import is tied to one specific Apify run and its dataset.

This prevents a concurrent scraper run from silently substituting a different dataset and allows each imported record to be traced back to the run that produced it.

### Multi-key deduplication

Records are normalized and deduplicated using multiple identifiers, including:

- post ID
- canonical URL
- fingerprint
- author/context information where applicable

### Human decisions are protected

Human-owned sales fields are treated as read-only by the pipeline.

A lead that is already progressing through the sales funnel is not reclassified in a way that overwrites its recorded outcome.

### Dry-run support

`DRY_RUN=true` exercises the real evaluation path while preventing Airtable writes and paid scraper starts.

This makes production validation possible before enabling mutations.

### Spend and write budgets

Separate controls exist for:

- records sent to the model
- actual OpenAI API requests, including retries
- historical records scanned
- Airtable records updated
- Airtable records created

The controls are intentionally separate because AI spend and database mutations are different operational risks.

### Non-idempotent write protection

Potentially ambiguous `POST` failures are not blindly replayed.

The pipeline distinguishes retry-safe failures from failures where the remote service may already have accepted the write, reducing the risk of duplicate Airtable records or duplicate paid scraper runs.

### Privacy-aware logs

Operational logs avoid printing raw Facebook post URLs, author names, post text, or full Airtable record IDs.

Records are represented with short keyed fingerprints so debugging remains possible without exposing source data in public workflow logs.

### Failure-safe outreach

If reprocessing fails, actionable outreach fields are withdrawn rather than leaving stale messaging active.

The previous analytical result remains available for diagnosis, but the failed record is not presented as safe to contact.

---

## Project Structure

```text
.
├── run_pipeline.py
├── qualification.py
├── intent.py
├── normalization.py
├── apify_runs.py
├── scraper_runs.py
├── group_performance.py
├── ai_retry.py
├── airtable_formulas.py
├── requirements.txt
├── requirements-dev.txt
├── tests/
├── docs/
└── .github/
    └── workflows/
        ├── facebook-leads.yml
        └── tests.yml
```

| File | Purpose |
|---|---|
| `run_pipeline.py` | Pipeline orchestration, Airtable integration, queueing, writes |
| `qualification.py` | Deterministic scoring, tiers, hard rejections, outreach gating |
| `intent.py` | Intent classification and service matching |
| `normalization.py` | URL normalization, fingerprints, deduplication |
| `apify_runs.py` | Apify run resolution and dataset retrieval |
| `scraper_runs.py` | Idempotent scraper-run logging |
| `group_performance.py` | Per-group funnel and performance aggregation |
| `ai_retry.py` | Cross-run AI retry state and retry policy |
| `airtable_formulas.py` | Airtable formula definitions used by the operational workflow |
| `tests/` | Regression tests with mocked external services |
| `docs/` | Detailed implementation and audit documentation |

---

## Technology Stack

- **Python 3.12**
- **OpenAI API**
- **Apify API**
- **Airtable API**
- **GitHub Actions**
- **pytest**
- **Requests**
- REST APIs
- JSON structured outputs
- regex-based deterministic classification

---

## Configuration

Production credentials are supplied through environment variables or GitHub Actions secrets.

### Required secrets

```text
APIFY_TOKEN
APIFY_TASK_ID
AIRTABLE_TOKEN
AIRTABLE_BASE_ID
AIRTABLE_TABLE_NAME
OPENAI_API_KEY
```

Never commit real credentials to the repository.

### Common runtime controls

| Variable | Default | Purpose |
|---|---:|---|
| `QUALIFICATION_THRESHOLD` | `65` | Minimum lead score for qualification |
| `MANUAL_REVIEW_THRESHOLD` | `55` | Minimum score for manual review |
| `HOT_LEAD_THRESHOLD` | `80` | Hot lead threshold |
| `MAX_POST_AGE_DAYS` | `5` | Reject posts older than this |
| `AI_BATCH_LIMIT` | `20` | Maximum records sent to the model |
| `PREFILTER_SCAN_LIMIT` | `200` | Historical backlog scan limit |
| `DRY_RUN` | `false` | Evaluate without operational writes |
| `RUN_APIFY_TASK` | `false` | Start and wait for a fresh scraper run |
| `APIFY_RUN_ID` | blank | Pin processing to one exact scraper run |

Additional safety, retry, outreach, and Airtable write-budget settings are documented in the source and `docs/`.

---

## Running Locally

### 1. Create a virtual environment

```bash
python -m venv .venv
```

macOS / Linux:

```bash
source .venv/bin/activate
```

Windows:

```powershell
.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

For development and testing:

```bash
pip install -r requirements-dev.txt
```

### 3. Set environment variables

macOS / Linux:

```bash
export APIFY_TOKEN="..."
export APIFY_TASK_ID="..."
export AIRTABLE_TOKEN="..."
export AIRTABLE_BASE_ID="..."
export AIRTABLE_TABLE_NAME="..."
export OPENAI_API_KEY="..."
```

### 4. Run the pipeline

```bash
python run_pipeline.py
```

---

## Safe Validation

Use dry-run mode before enabling writes against a production Airtable base:

```bash
DRY_RUN=true AI_BATCH_LIMIT=5 python run_pipeline.py
```

To process one specific Apify run:

```bash
APIFY_RUN_ID="<run-id>" DRY_RUN=true python run_pipeline.py
```

To explicitly start a new Apify task:

```bash
RUN_APIFY_TASK=true python run_pipeline.py
```

---

## Testing

The test suite is designed to run without production credentials.

```bash
python -m compileall -q .
python -m pytest -q
```

External services are mocked, so the test workflow does not:

- start paid Apify runs
- write to Airtable
- make live OpenAI requests

Regression coverage includes:

- qualification thresholds and tier boundaries
- hard rejection precedence
- intent classification
- service-match edge cases
- duplicate detection
- exact Apify run attribution
- retry handling
- write protection
- AI queue ordering
- dry-run behavior
- Airtable mutation budgets
- GitHub Actions configuration safety

CI runs on pushes and pull requests through:

```text
.github/workflows/tests.yml
```

The production pipeline workflow is kept separate from pull-request CI so a PR cannot accidentally trigger live external-service calls.

---

## GitHub Actions

The main operational workflow is:

```text
.github/workflows/facebook-leads.yml
```

The workflow supports controlled manual execution through GitHub Actions.

A typical validation flow is:

1. Run the test suite.
2. Start with dry-run enabled.
3. Use a small AI request and Airtable write budget.
4. Review the output.
5. Enable writes only after validating the result.

Concurrency protection prevents overlapping pipeline runs from mutating Airtable at the same time.

---

## Data and Security Notes

This repository is public for technical portfolio and collaboration purposes.

The repository should contain:

- source code
- tests
- documentation
- example configuration

It should **not** contain:

- API keys or access tokens
- Airtable credentials
- private customer or prospect data
- production exports
- raw scraped datasets
- private logs
- passwords or connection strings

Production secrets belong in GitHub Actions Secrets or local environment variables.

---

## Design Philosophy

This project intentionally avoids making an LLM the final decision-maker.

The model is best used for tasks that benefit from semantic understanding:

- extracting structured evidence
- interpreting business context
- drafting personalized outreach

Deterministic code is used for tasks that should remain consistent:

- qualification thresholds
- hard rejections
- service eligibility
- queue ordering
- retry limits
- write budgets
- sales-state protection

That separation makes the pipeline easier to test, audit, debug, and improve.

---

## Status

Active internal BruceTech project.

The pipeline is used to test and refine a repeatable lead-generation workflow while maintaining human review for sales decisions.

For detailed implementation history and production-readiness notes, see the files under `docs/` and `CHANGELOG.md`.
