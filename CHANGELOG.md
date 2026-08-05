# Changelog

All notable changes to the BruceTech Facebook lead pipeline.

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
