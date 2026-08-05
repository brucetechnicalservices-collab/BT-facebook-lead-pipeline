# Push Status

Implementation status for the deterministic qualification work.

## Branch

| | |
|---|---|
| Branch | `agent/improve-lead-qualification` |
| Base | `main` |
| Remote | `origin` (`brucetechnicalservices-collab/bt-facebook-lead-pipeline`) |
| Implementation commit | `ed0a819` — *Move lead qualification into deterministic Python and raise threshold to 65* |

## Pull request

| | |
|---|---|
| Number | [#1](https://github.com/brucetechnicalservices-collab/BT-facebook-lead-pipeline/pull/1) |
| Title | Deterministic lead qualification and 65-point threshold |
| State | **Draft** |
| Base | `main` |

## Files changed

13 files, 3,562 insertions, 435 deletions.

| File | Change | Lines |
|---|---|---|
| `qualification.py` | **new** — scoring, tiers, hard rejections, prefilter | +810 |
| `normalization.py` | **new** — URL canonicalisation, fingerprints, dedup | +356 |
| `run_pipeline.py` | rewritten — signals-only AI, lazy init, prioritisation | +1290 / −435 |
| `tests/test_qualification.py` | **new** | +446 |
| `tests/test_pipeline.py` | **new** | +303 |
| `tests/test_normalization.py` | **new** | +250 |
| `README.md` | rewritten from a one-line stub | +283 |
| `CHANGELOG.md` | **new** | +184 |
| `.github/workflows/facebook-leads.yml` | concurrency, test job, threshold 65 | +65 |
| `.gitignore` | **new** | +7 |
| `requirements-dev.txt` | **new** — pytest | +3 |
| `conftest.py` | **new** — puts the repo root on `sys.path` for pytest | 0 |
| `__pycache__/run_pipeline.cpython-312.pyc` | **deleted** — build artifact was tracked | −Bin |

## Validation completed

| Check | Command | Result |
|---|---|---|
| Compilation | `python -m compileall -q .` | ✅ Pass |
| Test suite | `python -m pytest -q` | ✅ 116 passed |
| Lazy init | Suite run with all six credential vars unset | ✅ Pass |
| Secret scan | Staged diff grepped for key/token/secret literals | ✅ Clean |
| Workflow YAML | `yaml.safe_load` on the workflow | ✅ Parses |
| Scoring weights | Component maximums sum check | ✅ Exactly 100 |

Python 3.11.15 locally; the workflow targets 3.12.

### Test results

```
116 passed in 0.18s
```

Coverage by area:

| Area | Tests |
|---|---|
| Threshold and tier boundaries (incl. 64 / 65) | 7 |
| Hard rejections (all 13 conditions) | 16 |
| Manual review behaviour | 2 |
| Outreach rules (no fallback DM, no channel upgrade) | 5 |
| Prefilter | 5 |
| Robustness / malformed input | 4 |
| URL normalisation | 21 |
| Post-ID extraction | 8 |
| Fingerprinting | 3 |
| Duplicate detection | 7 |
| Pipeline wiring, schema, prioritisation | 38 |

### Explicitly required regression cases

Every case named in the specification is covered:

| Requirement | Test |
|---|---|
| Score 64 does not qualify | `test_score_64_does_not_qualify` |
| Score 65 qualifies when no veto applies | `test_score_65_qualifies_when_no_veto_applies` |
| Hard rejection overrides a high score | `test_hard_rejection_overrides_high_score` |
| Resolved request is rejected | `test_resolved_request_is_rejected` |
| Promotional provider is rejected | `test_promotional_provider_is_rejected` |
| Personal consumer request is rejected | `test_personal_consumer_request_is_rejected` |
| Stale post is rejected | `test_stale_post_is_rejected` |
| No fallback DM is created | `test_no_fallback_dm_is_created` |
| URL normalisation and duplicate detection | `test_all_variants_normalize_to_one_canonical_url`, `test_duplicate_detected_across_url_variants` |

## Secrets

No credentials, tokens, or environment values were committed. The workflow
references secrets only through `${{ secrets.* }}`. Non-secret configuration
(thresholds, model name, batch limits) is set as plain workflow env values,
as before. `.gitignore` now excludes `.env`.

## Required before merge

1. **Create five Airtable fields**: `Lead Tier` (single line text),
   `Manual Review` (checkbox), `Outreach Ready` (checkbox), `Disqualifiers`
   (long text), `Prefilter Score` (number).
   Without them the pipeline warns on `UNKNOWN_FIELD_NAME` and writes only
   the core fields — it degrades rather than failing, but tiering and
   manual-review views will not work.
2. **Update the Outreach Ready view** to filter on
   `Outreach Ready = checked` instead of `Qualified`.

## Not verified

No live Apify, Airtable, or OpenAI call was made from this branch. The
following are covered by tests but have not been exercised against the real
services:

- The fresh Apify run mode (`APIFY_START_NEW_RUN`)
- The Airtable `UNKNOWN_FIELD_NAME` fallback path
- Real model output against the new strict signal schema

A manual `workflow_dispatch` run is recommended before relying on the daily
schedule.

Expect **fewer** outreach-ready leads after this change: the threshold rose
by 10 points, the generic fallback DM was removed, and `do_not_contact` is no
longer overridden. That is intended, but it is a visible drop in volume.
