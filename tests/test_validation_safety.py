"""
Validation-safety regression tests.

GitHub Actions run 32334487910 succeeded and proved almost nothing. It
scraped 50 items that were all duplicates, queued 200 records, rejected every
one of them as STALE_POST before any OpenAI call, and evaluated no newly
scraped post at all. Two things about it were worse than unhelpful:

* The public log printed complete Facebook post URLs, author names, and full
  Airtable record IDs for all 200 records.
* AI_BATCH_LIMIT was 5, and it bounded nothing that mattered. Deterministic
  rejections never reach the model, so a write-enabled version of that run
  would have modified 200 lead records with no ceiling anywhere in the
  pipeline stopping it.

These tests hold both fixes in place, plus the controls that make a narrowly
scoped validation run possible: an exposed deterministic scan limit and
switches for the two write paths that are not lead records.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pathlib

import pytest
import yaml

import apify_runs
import group_performance
import redaction
import run_pipeline as rp
import scraper_runs
from tests.fixtures import (
    GOHIGHLEVEL_PROVIDER,
    PEN_AND_PAPER_ELECTRICIAN,
)

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
MAX_AGE = 5
STALE_DAYS = 20
FRESH_DAYS = 1

#: A realistic Airtable record ID: the type prefix and fourteen characters.
#: The tests use real-shaped IDs because a short stub like "recStale" would
#: not exercise the pattern that finds one inside somebody else's string.
RECORD_ID_RE = re.compile(r"\brec[A-Za-z0-9]{14}\b")

#: Any Facebook URL, however it is spelled.
FACEBOOK_URL_RE = re.compile(r"https?://\S*facebook\.com/\S*", re.IGNORECASE)

AUTHOR_NAME = "Priya Ramaswamy"


def record_id(index: int) -> str:
    """A distinct, correctly shaped Airtable record ID."""
    return f"rec{index:014d}"


def post_url(index: int) -> str:
    return f"https://www.facebook.com/groups/998877/posts/{index:09d}/"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class CallCounter:
    """Counts OpenAI client construction and request attempts."""

    def __init__(self, response_json: dict | None = None):
        self.clients_built = 0
        self.requests_made = 0
        self._response_json = response_json

    def __call__(self):
        self.clients_built += 1
        return _FakeClient(self)


class _FakeClient:
    def __init__(self, counter: CallCounter):
        self.responses = _FakeResponses(counter)


class _FakeResponses:
    def __init__(self, counter: CallCounter):
        self._counter = counter

    def create(self, **kwargs):
        self._counter.requests_made += 1

        if self._counter._response_json is None:
            raise AssertionError("The pipeline must not call OpenAI here.")

        return _FakeResponse(json.dumps(self._counter._response_json))


class _FakeResponse:
    def __init__(self, output_text: str):
        self.status = "completed"
        self.output_text = output_text


class PatchRecorder:
    """
    Stands in for request_with_retry and counts real record mutations.

    Counting here rather than at update_airtable_records is deliberate: it
    is the only place that can prove batching is not hiding anything. Ten
    records in one PATCH must count as ten.
    """

    def __init__(self):
        self.mutated_ids: list[str] = []
        self.calls = 0

    def __call__(self, method, url, **kwargs):
        self.calls += 1
        payload = (kwargs.get("json") or {}).get("records", [])
        self.mutated_ids.extend(
            record.get("id") for record in payload if record.get("id")
        )
        return _FakeHttpResponse()


class _FakeHttpResponse:
    status_code = 200

    def json(self):
        return {"records": []}


QUALIFYING_SIGNALS = {
    "intent_strength": "explicit",
    "service_categories": ["crm", "workflow_automation"],
    "problem_specificity": "detailed",
    "business_context": "small_business",
    "purchase_signal": "ready_to_buy",
    "urgency": "high",
    "business_impact": "high",
    "location": "toronto_gta",
    "decision_maker": True,
    "competitor_mentioned": False,
    "classification_confidence": 0.9,
    "lead_summary": "Owner wants a CRM rebuilt.",
    "evidence": ["needs a CRM"],
    "suggested_dm": "Happy to help with the CRM rebuild.",
    "suggested_comment": "We do this often. Sending you a note.",
    "recommended_channel": "direct_message",
}


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_budgets():
    """Module-level counters must not leak between tests."""
    rp.reset_lead_update_budget()
    rp.reset_openai_request_budget()
    yield
    rp.reset_lead_update_budget()
    rp.reset_openai_request_budget()


@pytest.fixture
def airtable_env(monkeypatch):
    """Fake Airtable configuration. No request ever leaves the process."""
    monkeypatch.setenv("AIRTABLE_TOKEN", "fake-token-not-a-secret")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appTESTTESTTEST0")
    monkeypatch.setenv("AIRTABLE_TABLE_NAME", "Facebook Raw Signals")
    return monkeypatch


def make_record(index: int, *, text: str, days_old: float) -> dict:
    return {
        "id": record_id(index),
        "fields": {
            rp.FIELD_TEXT: text,
            rp.FIELD_TIME: (NOW - timedelta(days=days_old)).isoformat(),
            rp.FIELD_URL: post_url(index),
            rp.FIELD_USER_NAME: AUTHOR_NAME,
            rp.FIELD_COMMENTS: 3,
        },
    }


def stale_queue(size: int) -> list[dict]:
    return [
        make_record(index, text=GOHIGHLEVEL_PROVIDER, days_old=STALE_DAYS)
        for index in range(1, size + 1)
    ]


def fresh_queue(size: int) -> list[dict]:
    return [
        make_record(index, text=GOHIGHLEVEL_PROVIDER, days_old=FRESH_DAYS)
        for index in range(1, size + 1)
    ]


def run_queue(
    monkeypatch,
    records,
    *,
    response_json=None,
    budget=0,
    dry_run=False,
    recorder=None,
):
    """Put records through the real queue path. Returns (summary, counter)."""
    counter = CallCounter(response_json)

    # update_airtable_records pauses 0.25s between batches to stay inside
    # Airtable's rate limit. Correct in production, pure wall-clock here.
    monkeypatch.setattr(rp.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(rp, "MAX_POST_AGE_DAYS", MAX_AGE)
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_UPDATE_BUDGET", budget)
    monkeypatch.setattr(rp, "DRY_RUN", dry_run)
    monkeypatch.setattr(rp, "get_openai_client", counter)
    monkeypatch.setattr(
        rp, "request_with_retry", recorder or PatchRecorder()
    )

    prioritized = rp.prioritize_ai_queue(records, set(), now=NOW)
    summary = rp.process_ai_queue(prioritized, now=NOW)

    return summary, counter


# ===========================================================================
# 1. Redacted logs
# ===========================================================================

def test_no_facebook_url_reaches_a_qualification_log(
    monkeypatch, capsys, airtable_env
):
    """The exact leak in run 32334487910, for the stale path it took."""
    records = stale_queue(12)

    run_queue(monkeypatch, records)

    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert "facebook.com" not in output
    assert FACEBOOK_URL_RE.search(output) is None
    for index in range(1, 13):
        assert post_url(index) not in output


def test_no_facebook_url_reaches_a_dry_run_log(monkeypatch, capsys):
    records = stale_queue(12)

    run_queue(monkeypatch, records, dry_run=True)

    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert "facebook.com" not in output
    assert FACEBOOK_URL_RE.search(output) is None


def test_no_full_airtable_record_id_reaches_a_dry_run_update_log(
    monkeypatch, capsys
):
    """
    "[DRY RUN] Would update recXXXXXXXXXXXXXX" printed a live record ID for
    every record in the queue.
    """
    records = stale_queue(12)

    run_queue(monkeypatch, records, dry_run=True)

    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert "[DRY RUN] Would update" in output
    assert RECORD_ID_RE.search(output) is None
    for index in range(1, 13):
        assert record_id(index) not in output


def test_no_full_airtable_record_id_reaches_a_live_qualification_log(
    monkeypatch, capsys, airtable_env
):
    records = stale_queue(12)

    run_queue(monkeypatch, records)

    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert RECORD_ID_RE.search(output) is None


def test_no_author_name_reaches_a_qualification_log(
    monkeypatch, capsys, airtable_env
):
    records = fresh_queue(3)

    summary, counter = run_queue(
        monkeypatch, records, response_json=QUALIFYING_SIGNALS
    )

    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert counter.requests_made == 3, "the qualified path must be exercised"
    assert AUTHOR_NAME not in output


def test_no_post_text_reaches_a_qualification_log(
    monkeypatch, capsys, airtable_env
):
    records = [
        make_record(1, text=PEN_AND_PAPER_ELECTRICIAN, days_old=FRESH_DAYS)
    ]

    run_queue(monkeypatch, records, response_json=QUALIFYING_SIGNALS)

    captured = capsys.readouterr()
    output = captured.out + captured.err

    # Any sentence-length run of the post would be a leak; check the
    # distinctive phrases rather than the whole blob.
    for phrase in PEN_AND_PAPER_ELECTRICIAN.split(".")[:3]:
        phrase = phrase.strip()
        if len(phrase) > 20:
            assert phrase not in output


def test_the_queue_log_still_carries_the_operational_facts(
    monkeypatch, capsys, airtable_env
):
    """Redaction must not cost the operator anything they actually use."""
    records = stale_queue(3)

    run_queue(monkeypatch, records)

    output = capsys.readouterr().out

    assert "[1/3]" in output, "queue position"
    assert "[3/3]" in output
    assert "STALE_POST" in output, "machine-readable rejection code"
    assert "intent=" in output
    assert "score=" in output
    assert "rec:" in output, "a fingerprint to correlate lines by"


def test_two_lines_about_one_record_share_a_fingerprint(monkeypatch, capsys):
    """A fingerprint is only useful if it is stable within the run."""
    records = stale_queue(1)

    run_queue(monkeypatch, records, dry_run=True)

    output = capsys.readouterr().out
    fingerprints = re.findall(r"rec:([0-9a-f]{8})", output)

    assert len(fingerprints) >= 2, "the queue line and the dry-run write line"
    assert len(set(fingerprints)) == 1


def test_the_import_dry_run_lists_positions_not_urls(monkeypatch, capsys):
    monkeypatch.setattr(rp, "DRY_RUN", True)
    posts = [
        {"url": post_url(index), "text": "hello", "postId": str(index)}
        for index in range(1, 4)
    ]

    created = rp.create_new_posts_in_airtable(posts, apify_run_id="abc123")

    output = capsys.readouterr().out

    assert created == []
    assert "facebook.com" not in output
    assert "[1/3]" in output
    assert "post:" in output


GROUP_FIELDS = group_performance.RawSignalFieldNames(
    group_title="Group title",
    text="Text",
    qualified="Qualified",
    outreach_ready="Outreach Ready",
    outreach_status="Outreach Status",
    legacy_contacted="Contacted",
    evidence="Evidence",
    intent_type="Intent Type",
    scraper_run="Scraper Run",
)

GROUP_NAME = "Toronto Small Business Owners"


def test_group_performance_dry_run_redacts_the_record_id(capsys):
    """The group table's own dry-run listing printed a live record ID too."""
    written: list[list[dict]] = []

    group_performance.refresh_group_performance(
        raw_signal_lister=lambda **_: [
            {
                "id": record_id(1),
                "fields": {"Group title": GROUP_NAME, "Qualified": True},
            }
        ],
        performance_lister=lambda **_: [
            {
                "id": record_id(7),
                "fields": {group_performance.FIELD_GROUP_NAME: GROUP_NAME},
            }
        ],
        performance_updater=written.append,
        fields=GROUP_FIELDS,
        dry_run=True,
    )

    output = capsys.readouterr().out

    assert written == []
    assert "[DRY RUN] Would update group" in output
    assert "[1/1]" in output
    assert RECORD_ID_RE.search(output) is None
    assert record_id(7) not in output


APIFY_RUN = apify_runs.ApifyRun(
    id="BnHEoWoDCZDPnM8iY",
    status="SUCCEEDED",
    dataset_id="dsFAKEDATASET1",
    started_at="2026-08-20T04:00:00.000Z",
    finished_at="2026-08-20T04:10:00.000Z",
    item_count=50,
    cost_usd=0.42,
)


def test_scraper_run_logging_redacts_the_created_record_id(capsys):
    scraper_runs.upsert_scraper_run(
        APIFY_RUN,
        lister=lambda **_: [],
        creator=lambda payload: [{"id": record_id(3)}],
        updater=lambda payload: None,
    )

    output = capsys.readouterr().out

    assert "Logged Apify run" in output
    assert APIFY_RUN.id in output, "the Apify run ID is not sensitive"
    assert RECORD_ID_RE.search(output) is None
    assert record_id(3) not in output


def test_scraper_run_logging_redacts_the_updated_record_id(capsys):
    scraper_runs.upsert_scraper_run(
        APIFY_RUN,
        lister=lambda **_: [{"id": record_id(4), "fields": {}}],
        creator=lambda payload: [],
        updater=lambda payload: None,
    )

    output = capsys.readouterr().out

    assert "Updated existing scraper run record" in output
    assert RECORD_ID_RE.search(output) is None
    assert record_id(4) not in output


def test_the_ai_error_path_redacts_the_record_and_scrubs_the_error(
    monkeypatch, capsys, airtable_env
):
    """
    A third-party exception is not ours to trust. It is scrubbed before it
    reaches the log, so an Airtable error body quoting a record ID or a
    Facebook URL cannot leak through the error path.
    """
    leaky = f"upstream said {record_id(42)} at {post_url(42)} failed"

    class Exploding(CallCounter):
        def __call__(self):
            raise RuntimeError(leaky)

    monkeypatch.setattr(rp, "MAX_POST_AGE_DAYS", MAX_AGE)
    monkeypatch.setattr(rp, "DRY_RUN", True)
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_UPDATE_BUDGET", 0)
    monkeypatch.setattr(rp, "get_openai_client", Exploding())

    prioritized = rp.prioritize_ai_queue(fresh_queue(1), set(), now=NOW)
    summary = rp.process_ai_queue(prioritized, now=NOW)

    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert summary.ai_errors == 1
    assert RECORD_ID_RE.search(output) is None
    assert "facebook.com" not in output


# ---------------------------------------------------------------------------
# The redaction module itself
# ---------------------------------------------------------------------------

def test_a_fingerprint_is_stable_within_the_process():
    assert redaction.fingerprint("abc") == redaction.fingerprint("abc")


def test_different_values_get_different_fingerprints():
    assert redaction.fingerprint("abc") != redaction.fingerprint("abd")


def test_a_fingerprint_never_contains_the_input():
    url = post_url(1)

    assert url not in redaction.fingerprint(url)
    assert "998877" not in redaction.fingerprint(url)


def test_a_fingerprint_is_short_and_hexadecimal():
    assert re.fullmatch(r"[0-9a-f]{8}", redaction.fingerprint("anything"))


def test_an_empty_value_gets_a_marker_not_a_hash():
    assert redaction.fingerprint("") == redaction.EMPTY_MARKER
    assert redaction.fingerprint(None) == redaction.EMPTY_MARKER


def test_the_salt_is_random_unless_it_is_pinned(monkeypatch):
    """
    An unsalted hash of a Facebook URL is reversible by anyone holding the
    group's posts. Salting makes a published log unmatchable.
    """
    monkeypatch.delenv(redaction._ENV_SALT, raising=False)
    redaction.reset_salt()
    first = redaction.fingerprint("https://www.facebook.com/x")

    redaction.reset_salt()
    second = redaction.fingerprint("https://www.facebook.com/x")

    redaction.reset_salt()
    assert first != second


def test_a_pinned_salt_makes_fingerprints_reproducible(monkeypatch):
    monkeypatch.setenv(redaction._ENV_SALT, "pinned-for-correlation")
    redaction.reset_salt()
    first = redaction.fingerprint("https://www.facebook.com/x")

    redaction.reset_salt()
    second = redaction.fingerprint("https://www.facebook.com/x")

    redaction.reset_salt()
    assert first == second


@pytest.mark.parametrize(
    "text",
    [
        "https://www.facebook.com/groups/1/posts/2/",
        "http://m.facebook.com/story.php?id=9",
        "see https://facebook.com/permalink/3 for details",
        "https://fb.me/abc",
    ],
)
def test_scrub_removes_every_shape_of_facebook_url(text):
    scrubbed = redaction.scrub(text)

    assert "facebook.com" not in scrubbed
    assert "fb.me" not in scrubbed
    assert "post:" in scrubbed


def test_scrub_removes_airtable_record_base_and_table_ids():
    text = (
        f"{record_id(1)} in appABCDEFGHIJKLMN "
        f"table tblABCDEFGHIJKLMN"
    )

    scrubbed = redaction.scrub(text)

    assert RECORD_ID_RE.search(scrubbed) is None
    assert "appABCDEFGHIJKLMN" not in scrubbed
    assert "tblABCDEFGHIJKLMN" not in scrubbed


def test_scrub_removes_synthetic_dry_run_ids():
    scrubbed = redaction.scrub(f"{rp.DRY_RUN_RECORD_PREFIX}0007 failed")

    assert rp.DRY_RUN_RECORD_PREFIX not in scrubbed


def test_scrub_truncates_to_the_limit():
    assert len(redaction.scrub("x" * 500, limit=50)) == 53


def test_scrub_leaves_ordinary_text_alone():
    text = "STALE_POST intent=BUSINESS_PAIN score=42 attempt 2/3"

    assert redaction.scrub(text) == text


# ===========================================================================
# 2. The deterministic scan limit is exposed and honoured
# ===========================================================================

class FakeAirtable:
    """Records every formula it was asked for, and returns nothing."""

    def __init__(self):
        self.formulas: list[str] = []

    def __call__(self, *, formula, fields=None, **kwargs):
        self.formulas.append(formula)
        return []


def test_a_zero_scan_limit_stops_historical_backlog_scanning(monkeypatch):
    airtable = FakeAirtable()
    monkeypatch.setattr(rp, "PREFILTER_SCAN_LIMIT", 0)
    monkeypatch.setattr(rp, "list_airtable_records", airtable)

    rp.fetch_ai_queue([])

    joined = " ".join(airtable.formulas)
    assert "Send to AI" not in joined, "the formula-prequalified phase"
    assert len(airtable.formulas) == 1, "human decisions only"


def test_a_zero_scan_limit_still_evaluates_explicit_human_decisions(
    monkeypatch,
):
    airtable = FakeAirtable()
    monkeypatch.setattr(rp, "PREFILTER_SCAN_LIMIT", 0)
    monkeypatch.setattr(rp, "list_airtable_records", airtable)

    rp.fetch_ai_queue([])

    assert any(
        rp.HUMAN_DECISION_APPROVE in formula for formula in airtable.formulas
    )


def test_a_zero_scan_limit_still_evaluates_this_runs_imports(monkeypatch):
    """
    The whole point of a scoped validation run: look at today's scrape and
    nothing else. Current-run records are fetched by ID, never by the
    backlog query, so the limit must not touch them.
    """
    imported = [record_id(index) for index in range(1, 51)]
    monkeypatch.setattr(rp, "PREFILTER_SCAN_LIMIT", 0)
    monkeypatch.setattr(rp, "list_airtable_records", FakeAirtable())
    monkeypatch.setattr(
        rp,
        "fetch_records_by_id",
        lambda ids: [
            make_record(int(rid[3:]), text=GOHIGHLEVEL_PROVIDER, days_old=1)
            for rid in ids
        ],
    )

    queue = rp.fetch_ai_queue(imported)

    assert len(queue) == 50


def test_the_default_scan_limit_is_unchanged_at_two_hundred():
    assert rp.DEFAULT_PREFILTER_SCAN_LIMIT == 200


def test_a_blank_scan_limit_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("PREFILTER_SCAN_LIMIT", "")

    assert rp.env_int(
        "PREFILTER_SCAN_LIMIT", rp.DEFAULT_PREFILTER_SCAN_LIMIT
    ) == 200


# ===========================================================================
# 3. The lead-update budget
# ===========================================================================

def test_two_hundred_stale_records_with_a_budget_of_three_write_three(
    monkeypatch, airtable_env
):
    """
    Run 32334487910, write-enabled, with the ceiling this adds.

    Counted at the HTTP layer, so a batch of ten cannot pass as one.
    """
    recorder = PatchRecorder()

    summary, counter = run_queue(
        monkeypatch, stale_queue(200), budget=3, recorder=recorder
    )

    assert summary.queue_size == 200
    assert counter.requests_made == 0, "no record reached OpenAI"
    assert len(recorder.mutated_ids) == 3
    assert len(set(recorder.mutated_ids)) == 3
    assert rp.lead_updates_made() == 3


def test_the_remaining_records_are_reported_as_deferred_not_updated(
    monkeypatch, airtable_env
):
    summary, _ = run_queue(monkeypatch, stale_queue(200), budget=3)

    assert summary.records_evaluated == 200
    assert summary.update_candidates == 200
    assert summary.updates_written == 3
    assert summary.deferred_no_update_budget == 197
    assert summary.lead_update_budget_exhausted is True
    assert summary.updates_written + summary.deferred_no_update_budget == (
        summary.update_candidates
    )


def test_the_summary_says_the_budget_stopped_the_run(
    monkeypatch, airtable_env
):
    summary, _ = run_queue(monkeypatch, stale_queue(20), budget=3)

    rendered = summary.render()

    assert "Updates written" in rendered
    assert "Deferred (no budget)" in rendered
    assert "BUDGET EXHAUSTED" in rendered


def test_an_evaluation_outcome_is_not_a_write(monkeypatch, airtable_env):
    """
    Rejected counts decisions; updates_written counts records changed. They
    were the same number before the budget existed, and conflating them is
    how "we rejected 200" reads as harmless.
    """
    summary, _ = run_queue(monkeypatch, stale_queue(200), budget=3)

    assert summary.rejected == 200
    assert summary.stale_skipped == 200
    assert summary.updates_written == 3


def test_no_budget_means_unlimited_which_is_production_behaviour(
    monkeypatch, airtable_env
):
    recorder = PatchRecorder()

    summary, _ = run_queue(
        monkeypatch, stale_queue(200), budget=0, recorder=recorder
    )

    assert len(recorder.mutated_ids) == 200
    assert summary.updates_written == 200
    assert summary.deferred_no_update_budget == 0
    assert summary.lead_update_budget_exhausted is False


def test_the_application_default_is_unlimited():
    assert rp.DEFAULT_AIRTABLE_LEAD_UPDATE_BUDGET == 0


def test_a_blank_budget_input_is_unlimited(monkeypatch):
    """The workflow passes "" when the operator leaves the box alone."""
    monkeypatch.setenv("AIRTABLE_LEAD_UPDATE_BUDGET", "")

    assert rp.env_int(
        "AIRTABLE_LEAD_UPDATE_BUDGET",
        rp.DEFAULT_AIRTABLE_LEAD_UPDATE_BUDGET,
    ) == 0


def test_the_budget_counts_records_not_http_requests(
    monkeypatch, airtable_env
):
    """
    Updates flush in batches of ten. A budget of 25 must mean 25 records,
    which is three PATCH requests, not 25.
    """
    recorder = PatchRecorder()

    run_queue(monkeypatch, stale_queue(200), budget=25, recorder=recorder)

    assert len(recorder.mutated_ids) == 25
    assert recorder.calls == 3


def test_the_budget_covers_the_human_rejection_path(
    monkeypatch, airtable_env
):
    records = fresh_queue(4)
    for record in records:
        record["fields"][rp.FIELD_HUMAN_DECISION] = rp.HUMAN_DECISION_REJECT
    recorder = PatchRecorder()

    summary, _ = run_queue(
        monkeypatch, records, budget=1, recorder=recorder
    )

    assert summary.human_rejected == 4
    assert len(recorder.mutated_ids) == 1
    assert summary.deferred_no_update_budget == 3


def test_the_budget_covers_the_ordinary_prefilter_rejection_path(
    monkeypatch, airtable_env
):
    records = [
        make_record(index, text="Anyone know a good roofer?", days_old=1)
        for index in range(1, 11)
    ]
    recorder = PatchRecorder()

    summary, counter = run_queue(
        monkeypatch, records, budget=2, recorder=recorder
    )

    assert summary.prefilter_rejected == 10
    assert summary.stale_skipped == 0, "these are rejected on intent, not age"
    assert counter.requests_made == 0
    assert len(recorder.mutated_ids) == 2
    assert summary.deferred_no_update_budget == 8


def test_the_budget_covers_the_successful_qualification_path(
    monkeypatch, airtable_env
):
    recorder = PatchRecorder()

    summary, counter = run_queue(
        monkeypatch,
        fresh_queue(6),
        response_json=QUALIFYING_SIGNALS,
        budget=2,
        recorder=recorder,
    )

    assert summary.prefilter_accepted == 6
    assert len(recorder.mutated_ids) == 2
    assert summary.updates_written == 2
    assert summary.deferred_no_update_budget == 4


def test_the_budget_covers_the_ai_error_path(monkeypatch, airtable_env):
    class Exploding(CallCounter):
        def __call__(self):
            self.clients_built += 1
            raise RuntimeError("upstream is down")

    counter = Exploding()
    recorder = PatchRecorder()

    monkeypatch.setattr(rp, "MAX_POST_AGE_DAYS", MAX_AGE)
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_UPDATE_BUDGET", 2)
    monkeypatch.setattr(rp, "DRY_RUN", False)
    monkeypatch.setattr(rp, "get_openai_client", counter)
    monkeypatch.setattr(rp, "request_with_retry", recorder)

    prioritized = rp.prioritize_ai_queue(fresh_queue(6), set(), now=NOW)
    summary = rp.process_ai_queue(prioritized, now=NOW)

    assert summary.ai_errors == 2, "only the two the budget could record"
    assert len(recorder.mutated_ids) == 2
    assert summary.deferred_no_update_budget == 4


def test_ai_is_not_called_when_no_budget_remains_to_save_the_result(
    monkeypatch, airtable_env
):
    """
    Paying for a classification that cannot be written down is pure waste.
    The check happens before the request, not after it.
    """
    summary, counter = run_queue(
        monkeypatch,
        fresh_queue(10),
        response_json=QUALIFYING_SIGNALS,
        budget=3,
    )

    assert counter.requests_made == 3
    assert summary.ai_processed == 3
    assert rp.openai_requests_made() == 3
    assert summary.deferred_no_update_budget == 7


def test_a_record_deferred_by_the_budget_is_left_completely_untouched(
    monkeypatch, airtable_env
):
    recorder = PatchRecorder()

    run_queue(monkeypatch, stale_queue(10), budget=3, recorder=recorder)

    written = set(recorder.mutated_ids)
    deferred = {record_id(index) for index in range(4, 11)}

    assert written.isdisjoint(deferred)
    assert written == {record_id(index) for index in range(1, 4)}


def test_the_batch_limit_still_defers_separately_from_the_budget(
    monkeypatch, airtable_env
):
    monkeypatch.setattr(rp, "AI_BATCH_LIMIT", 2)

    summary, counter = run_queue(
        monkeypatch,
        fresh_queue(6),
        response_json=QUALIFYING_SIGNALS,
        budget=0,
    )

    assert counter.requests_made == 2
    assert summary.deferred_ai_batch_limit == 4
    assert summary.deferred_no_update_budget == 0


# ---------------------------------------------------------------------------
# Dry runs never spend it
# ---------------------------------------------------------------------------

def test_a_dry_run_evaluates_the_whole_queue_without_spending_the_budget(
    monkeypatch, capsys
):
    summary, _ = run_queue(
        monkeypatch, stale_queue(50), budget=3, dry_run=True
    )

    assert summary.records_evaluated == 50
    assert summary.update_candidates == 50
    assert summary.updates_written == 50, "simulated, not written"
    assert summary.deferred_no_update_budget == 0
    assert rp.lead_updates_made() == 0


def test_a_dry_run_sends_no_patch_at_all(monkeypatch):
    recorder = PatchRecorder()

    run_queue(
        monkeypatch,
        stale_queue(50),
        budget=3,
        dry_run=True,
        recorder=recorder,
    )

    assert recorder.mutated_ids == []
    assert recorder.calls == 0


def test_a_dry_run_summary_says_the_writes_were_simulated(monkeypatch):
    summary, _ = run_queue(
        monkeypatch, stale_queue(4), budget=3, dry_run=True
    )

    rendered = summary.render()

    assert "Updates simulated" in rendered
    assert "nothing written" in rendered.lower()
    assert "not spent (dry run)" in rendered
    assert "BUDGET EXHAUSTED" not in rendered


def test_the_budget_helpers_report_unlimited_as_none(monkeypatch):
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_UPDATE_BUDGET", 0)
    monkeypatch.setattr(rp, "DRY_RUN", False)

    assert rp.lead_update_budget_remaining() is None
    assert rp.has_lead_update_budget(10_000) is True


def test_the_budget_helpers_report_a_dry_run_as_unlimited(monkeypatch):
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_UPDATE_BUDGET", 3)
    monkeypatch.setattr(rp, "DRY_RUN", True)

    assert rp.lead_update_budget_remaining() is None
    assert rp.has_lead_update_budget(10_000) is True


def test_consuming_past_the_budget_raises(monkeypatch):
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_UPDATE_BUDGET", 2)
    monkeypatch.setattr(rp, "DRY_RUN", False)

    rp.consume_lead_update()
    rp.consume_lead_update()

    with pytest.raises(rp.LeadUpdateBudgetExhausted):
        rp.consume_lead_update()


def test_the_exhaustion_message_says_the_records_stay_pending(monkeypatch):
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_UPDATE_BUDGET", 1)
    monkeypatch.setattr(rp, "DRY_RUN", False)
    rp.consume_lead_update()

    with pytest.raises(rp.LeadUpdateBudgetExhausted) as caught:
        rp.consume_lead_update()

    assert "Pending" in str(caught.value)
    assert "AIRTABLE_LEAD_UPDATE_BUDGET" in str(caught.value)


# ===========================================================================
# 4 and 5. Workflow wiring
# ===========================================================================

WORKFLOW = ".github/workflows/facebook-leads.yml"


@pytest.fixture(scope="module")
def workflow():
    """
    The parsed pipeline workflow.

    PyYAML is imported at module scope, not with importorskip. It used to be
    optional here, and PyYAML is not a runtime dependency, so on a CI runner
    that installs only requirements-dev.txt every workflow assertion in this
    file skipped and the job still went green. Run 32450727941 reported
    "698 passed, 25 skipped" while the schedule-disabled check, the
    entry-point check, and every input-wiring check quietly did not run.
    A missing PyYAML must break collection instead.
    """
    with open(WORKFLOW, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def workflow_text():
    with open(WORKFLOW, encoding="utf-8") as handle:
        return handle.read()


def _triggers(workflow):
    # PyYAML parses the bare key `on:` as the boolean True.
    return workflow.get("on", workflow.get(True))


def _dispatch_inputs(workflow):
    return _triggers(workflow)["workflow_dispatch"]["inputs"]


def _pipeline_env(workflow):
    steps = workflow["jobs"]["run-pipeline"]["steps"]
    return next(step for step in steps if "env" in step)["env"]


def test_the_workflow_offers_the_scan_limit_as_an_input(workflow):
    assert "prefilter_scan_limit" in _dispatch_inputs(workflow)


def test_the_workflow_passes_the_scan_limit_to_the_pipeline(workflow):
    env = _pipeline_env(workflow)

    assert "PREFILTER_SCAN_LIMIT" in env
    assert "github.event.inputs.prefilter_scan_limit" in (
        env["PREFILTER_SCAN_LIMIT"]
    )


def test_the_scan_limit_default_matches_the_application_default(workflow):
    inputs = _dispatch_inputs(workflow)
    env = _pipeline_env(workflow)

    assert int(inputs["prefilter_scan_limit"]["default"]) == (
        rp.DEFAULT_PREFILTER_SCAN_LIMIT
    )
    assert f"'{rp.DEFAULT_PREFILTER_SCAN_LIMIT}'" in (
        env["PREFILTER_SCAN_LIMIT"]
    )


def test_the_scan_limit_input_says_what_it_does_not_control(workflow):
    """
    The name invites the reading "this is how much the run costs". It is
    not: it caps free regex evaluation of the backlog.
    """
    description = _dispatch_inputs(workflow)["prefilter_scan_limit"][
        "description"
    ].lower()

    assert "backlog" in description
    assert "ai" in description
    assert "import" in description


def test_the_workflow_offers_the_lead_update_budget_as_an_input(workflow):
    assert "airtable_lead_update_budget" in _dispatch_inputs(workflow)


def test_the_lead_update_budget_input_defaults_to_blank(workflow):
    """Blank is unlimited, which is what production has always done."""
    assert _dispatch_inputs(workflow)["airtable_lead_update_budget"][
        "default"
    ] == ""


def test_the_lead_update_budget_input_says_it_counts_records(workflow):
    description = _dispatch_inputs(workflow)["airtable_lead_update_budget"][
        "description"
    ].lower()

    assert "record" in description
    assert "unlimited" in description


def test_the_workflow_passes_the_lead_update_budget_to_the_pipeline(workflow):
    env = _pipeline_env(workflow)

    assert "AIRTABLE_LEAD_UPDATE_BUDGET" in env
    assert "github.event.inputs.airtable_lead_update_budget" in (
        env["AIRTABLE_LEAD_UPDATE_BUDGET"]
    )


def test_the_workflow_offers_both_unrelated_write_switches(workflow):
    inputs = _dispatch_inputs(workflow)

    assert "update_group_performance" in inputs
    assert "log_scraper_runs" in inputs


def test_both_write_switches_keep_their_current_defaults(workflow):
    inputs = _dispatch_inputs(workflow)

    assert inputs["update_group_performance"]["default"] is True
    assert inputs["log_scraper_runs"]["default"] is True


def test_both_write_switches_reach_the_pipeline(workflow):
    env = _pipeline_env(workflow)

    assert "github.event.inputs.update_group_performance" in (
        env["UPDATE_GROUP_PERFORMANCE"]
    )
    assert "github.event.inputs.log_scraper_runs" in env["LOG_SCRAPER_RUNS"]


def test_the_write_switches_read_the_string_context_not_the_boolean_one(
    workflow,
):
    """
    ``${{ inputs.log_scraper_runs || 'true' }}`` reads a real boolean, and
    an unticked box is false, so the fallback fires and the expression
    evaluates to 'true'. The control would silently do nothing.
    ``github.event.inputs.*`` is always a string, so 'false' survives.
    """
    env = _pipeline_env(workflow)

    for key in ("UPDATE_GROUP_PERFORMANCE", "LOG_SCRAPER_RUNS"):
        expression = env[key]
        assert "github.event.inputs." in expression
        assert not re.search(r"\{\{\s*inputs\.", expression)


def test_the_application_defaults_for_both_switches_are_still_on(monkeypatch):
    monkeypatch.delenv("LOG_SCRAPER_RUNS", raising=False)
    monkeypatch.delenv("UPDATE_GROUP_PERFORMANCE", raising=False)

    assert rp.env_bool("LOG_SCRAPER_RUNS", True) is True
    assert rp.env_bool("UPDATE_GROUP_PERFORMANCE", True) is True


def test_unticking_a_write_switch_actually_disables_it(monkeypatch):
    """The string the workflow sends must read as off, not as a non-empty
    string that env_bool waves through."""
    monkeypatch.setenv("LOG_SCRAPER_RUNS", "false")
    monkeypatch.setenv("UPDATE_GROUP_PERFORMANCE", "false")

    assert rp.env_bool("LOG_SCRAPER_RUNS", True) is False
    assert rp.env_bool("UPDATE_GROUP_PERFORMANCE", True) is False


# ---------------------------------------------------------------------------
# Blank inputs preserve production behaviour
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,default",
    [
        ("AI_BATCH_LIMIT", 20),
        ("OPENAI_REQUEST_BUDGET", 60),
        ("PREFILTER_SCAN_LIMIT", 200),
        ("AIRTABLE_LEAD_UPDATE_BUDGET", 0),
    ],
)
def test_a_blank_input_leaves_the_application_default_in_place(
    monkeypatch, name, default
):
    monkeypatch.setenv(name, "")

    assert rp.env_int(name, default) == default


def test_the_default_configuration_is_the_pre_existing_behaviour():
    """
    Nothing here changes what a normal run does. New knobs, same defaults.
    """
    assert rp.DEFAULT_AI_BATCH_LIMIT == 20
    assert rp.DEFAULT_PREFILTER_SCAN_LIMIT == 200
    assert rp.DEFAULT_AIRTABLE_LEAD_UPDATE_BUDGET == 0
    assert rp.env_bool("LOG_SCRAPER_RUNS", True) is True
    assert rp.env_bool("UPDATE_GROUP_PERFORMANCE", True) is True


# ---------------------------------------------------------------------------
# The README names the same numbers the code does
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def readme():
    with open("README.md", encoding="utf-8") as handle:
        return handle.read()


def _env_table_row(readme_text: str, variable: str) -> str:
    """
    The row for one variable in the "Environment variables" reference.

    Scoped to that section on purpose: several variables also appear in the
    explanatory tables above it, which carry no default column.
    """
    _, _, reference = readme_text.partition("\n## Environment variables")
    assert reference, "README has no Environment variables section"

    for line in reference.splitlines():
        if line.startswith(f"| `{variable}` |"):
            return line

    raise AssertionError(f"README has no environment row for {variable}")


def test_the_readme_documents_the_lead_update_budget(readme):
    """
    The workflow default, the Python default, and the README drifted once
    before on AI_BATCH_LIMIT. Assert rather than trust.
    """
    row = _env_table_row(readme, "AIRTABLE_LEAD_UPDATE_BUDGET")

    assert f"`{rp.DEFAULT_AIRTABLE_LEAD_UPDATE_BUDGET}`" in row
    assert "unlimited" in row.lower()
    assert "record" in row.lower()


def test_the_readme_documents_the_scan_limit_default(readme):
    row = _env_table_row(readme, "PREFILTER_SCAN_LIMIT")

    assert f"`{rp.DEFAULT_PREFILTER_SCAN_LIMIT}`" in row
    assert "`0`" in row


def test_the_readme_documents_the_fingerprint_salt(readme):
    row = _env_table_row(readme, "LOG_FINGERPRINT_SALT")

    assert redaction._ENV_SALT == "LOG_FINGERPRINT_SALT"
    assert "random" in row.lower()


def test_the_readme_lists_what_the_logs_never_print(readme):
    assert "### What the logs may say" in readme
    for forbidden in ("Post URL", "Post text", "Author name"):
        assert forbidden in readme


# ---------------------------------------------------------------------------
# The workflow tests must actually run
#
# They parse the workflow through PyYAML, which is not a runtime dependency.
# When that import was optional, a CI runner without PyYAML skipped all 25 of
# them and the job reported success: run 32450727941 said "698 passed, 25
# skipped" while the schedule-disabled check and every input-wiring check sat
# out. A test that can silently not run is not a test.
# ---------------------------------------------------------------------------

DEV_REQUIREMENTS = "requirements-dev.txt"
PROD_REQUIREMENTS = "requirements.txt"


def _requirement_names(path: str) -> set[str]:
    """Distribution names declared in a requirements file, lowercased."""
    names = set()

    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            name = re.split(r"[<>=!~\[;]", line, maxsplit=1)[0]
            names.add(name.strip().lower())

    return names


def test_pyyaml_is_a_declared_development_dependency():
    assert "pyyaml" in _requirement_names(DEV_REQUIREMENTS)


def test_pyyaml_is_pinned_below_the_next_major():
    """A PyYAML 7 that changed safe_load would be caught, not absorbed."""
    with open(DEV_REQUIREMENTS, encoding="utf-8") as handle:
        text = handle.read()

    assert "PyYAML>=6.0,<7.0" in text


def test_pyyaml_is_not_a_production_dependency():
    """
    The pipeline itself never parses YAML. Fixing the CI gap must not put a
    new package into the runtime environment.
    """
    assert "pyyaml" not in _requirement_names(PROD_REQUIREMENTS)


def _optional_yaml_imports() -> list[str]:
    """
    Every ``importorskip`` in the test suite that would make YAML optional.

    Walked as a syntax tree rather than grepped, so the explanatory comments
    and docstrings that mention importorskip cannot satisfy this test.
    """
    import ast

    found = []

    for path in sorted(pathlib.Path("tests").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name != "importorskip":
                continue

            for arg in node.args:
                if isinstance(arg, ast.Constant) and "yaml" in str(
                    arg.value
                ).lower():
                    found.append(f"{path}:{node.lineno}")

    return found


def test_no_test_module_treats_yaml_as_optional():
    assert _optional_yaml_imports() == []


def test_every_workflow_parsing_module_imports_yaml_at_module_scope():
    """
    The positive half of the rule above. A module that reads the workflow
    must fail collection without PyYAML, not skip.
    """
    import ast

    checked = 0

    for path in sorted(pathlib.Path("tests").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "yaml.safe_load" not in source:
            continue

        checked += 1
        tree = ast.parse(source)
        module_level = {
            alias.name
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
        }

        assert "yaml" in module_level, (
            f"{path} parses YAML but does not import it at module scope, so "
            f"a runner without PyYAML would skip its tests instead of failing"
        )

    assert checked == 3, (
        "expected exactly three workflow-parsing test modules; a new one "
        "must import yaml at module scope like the others"
    )


# ---------------------------------------------------------------------------
# The workflow is still the workflow
# ---------------------------------------------------------------------------

def test_the_schedule_is_still_disabled(workflow):
    assert "schedule" not in _triggers(workflow)


def test_the_schedule_is_commented_out_not_deleted(workflow_text):
    """It must stay recoverable, and it must stay off."""
    assert "# schedule:" in workflow_text
    assert re.search(r"^\s*schedule:", workflow_text, re.MULTILINE) is None


def test_the_workflow_still_runs_the_real_pipeline_entry_point(workflow):
    steps = workflow["jobs"]["run-pipeline"]["steps"]
    commands = [step.get("run", "") for step in steps]

    assert "python run_pipeline.py" in commands


def test_the_test_job_still_compiles_and_tests_everything(workflow):
    steps = workflow["jobs"]["test"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)

    assert "compileall" in commands
    assert "pytest" in commands


def test_the_pipeline_job_still_waits_for_the_tests(workflow):
    assert workflow["jobs"]["run-pipeline"]["needs"] == "test"


def test_runs_still_cannot_overlap(workflow):
    assert workflow["concurrency"]["cancel-in-progress"] is False
