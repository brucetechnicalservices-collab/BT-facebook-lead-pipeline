"""
Queue coverage regression tests from the 2026-08-19 Blue Collar live run.

Apify run BnHEoWoDCZDPnM8iY, dataset MiUEsgX1KSeiGuUyH, 50 posts imported.
Only 25 received deterministic Python evaluation. The other 25 were never
looked at, and one of them was a strong lead.

THE CAUSE

``fetch_ai_queue`` sized its window as ``AI_BATCH_LIMIT * 5``. The run set
AI_BATCH_LIMIT=5, so the window was 25. A spend limit was silently rationing
*evaluation*, which costs nothing but regex.

THE ARCHITECTURE THESE TESTS PIN

    every current-run record
        -> deterministic Python evaluation (free, unbounded for this run)
        -> eligible candidates sorted by intent and score
        -> AI_BATCH_LIMIT caps OPENAI CALLS ONLY

PREFILTER_SCAN_LIMIT bounds the historical backlog read, and nothing else.
Current-run records are fetched by record ID so no Airtable ordering, page
boundary, or scan limit can hide one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import run_pipeline as rp
from tests.fixtures import (
    GENERAL_GROWTH_QUESTION,
    GOHIGHLEVEL_PROVIDER,
    SURFACE_RESTORATION_LEAD_GEN,
    TRAILER_SIZE_QUESTION,
)

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

#: The live run's setting. Every test here uses it.
LIVE_AI_BATCH_LIMIT = 5

#: The live run's scrape size.
CURRENT_RUN_SIZE = 50


def make_record(record_id: str, *, text: str, days_old: float = 1) -> dict:
    return {
        "id": record_id,
        "fields": {
            rp.FIELD_TEXT: text,
            rp.FIELD_TIME: (NOW - timedelta(days=days_old)).isoformat(),
            rp.FIELD_URL: f"https://www.facebook.com/groups/1/posts/{record_id}",
            rp.FIELD_COMMENTS: 3,
        },
    }


def current_run_records(
    *,
    size: int = CURRENT_RUN_SIZE,
    strong_at: int | None = None,
) -> list[dict]:
    """
    A current run of `size` posts, optionally with one strong lead planted.

    The filler is a post that is correctly rejected before the AI, so any AI
    call in these tests is attributable to the planted record.
    """
    records = [
        make_record(f"recRun{index:03d}", text=TRAILER_SIZE_QUESTION)
        for index in range(size)
    ]

    if strong_at is not None:
        records[strong_at] = make_record(
            f"recRun{strong_at:03d}", text=GOHIGHLEVEL_PROVIDER
        )

    return records


class FakeAirtable:
    """Stands in for the Airtable list endpoint, recording every query."""

    def __init__(self, by_id: list[dict], backlog: list[dict] | None = None):
        self.by_id = {record["id"]: record for record in by_id}
        self.backlog = backlog or []
        self.queries: list[str] = []

    def __call__(self, *, formula, fields, max_records=None, **kwargs):
        self.queries.append(formula)

        if "RECORD_ID()" in formula:
            wanted = [
                rid for rid in self.by_id if f"RECORD_ID()='{rid}'" in formula
            ]
            return [self.by_id[rid] for rid in wanted]

        if rp.FIELD_HUMAN_DECISION in formula or "Send to AI" in formula:
            return []

        records = list(self.backlog)
        if max_records is not None:
            records = records[:max_records]
        return records


class CallCounter:
    """Counts OpenAI client construction and requests."""

    def __init__(self, response_json: dict):
        self.clients_built = 0
        self.requests_made = 0
        self._response_json = response_json

    def __call__(self):
        self.clients_built += 1
        return _FakeClient(self)


class _FakeClient:
    def __init__(self, counter):
        self.responses = _FakeResponses(counter)


class _FakeResponses:
    def __init__(self, counter):
        self._counter = counter

    def create(self, **kwargs):
        self._counter.requests_made += 1
        return _FakeResponse(json.dumps(self._counter._response_json))


class _FakeResponse:
    def __init__(self, output_text):
        self.status = "completed"
        self.output_text = output_text


QUALIFYING_SIGNALS = {
    "intent_strength": "explicit",
    "service_categories": ["crm", "workflow_automation"],
    "problem_specificity": "detailed",
    "business_context": "small_business",
    "purchase_signal": "ready_to_buy",
    "urgency": "high",
    "business_impact": "high",
    "location": "toronto_gta",
    "buyer_role": "owner_or_decision_maker",
    "resolved_status": "unresolved",
    "provider_already_selected": False,
    "personal_request": False,
    "free_only_request": False,
    "promotional_post": False,
    "competitor_or_agency": False,
    "spam_risk": "none",
    "outreach_appropriateness": "appropriate",
    "classification_confidence": 0.9,
    "disqualifier_codes": [],
    "lead_summary": "HVAC company wants GoHighLevel set up.",
    "evidence": "Asks for the whole setup.",
    "service_match": "CRM",
    "suggested_dm": "Hi, saw your post about GoHighLevel. We do this. Chat?",
    "recommended_channel": "direct_message",
}


@pytest.fixture
def live_run(monkeypatch):
    """The live run's configuration, with every external call faked."""
    monkeypatch.setattr(rp, "AI_BATCH_LIMIT", LIVE_AI_BATCH_LIMIT)
    monkeypatch.setattr(rp, "MAX_POST_AGE_DAYS", 5)
    monkeypatch.setattr(rp, "update_airtable_records", lambda batch: None)
    return monkeypatch


# ---------------------------------------------------------------------------
# Test 1 and 2 -- all 50 current-run records are evaluated
# ---------------------------------------------------------------------------

def test_the_current_run_contains_fifty_records():
    assert len(current_run_records()) == CURRENT_RUN_SIZE


def test_all_fifty_current_run_records_are_fetched_for_evaluation(live_run):
    records = current_run_records()
    airtable = FakeAirtable(records)
    live_run.setattr(rp, "list_airtable_records", airtable)

    queue = rp.fetch_ai_queue([record["id"] for record in records])

    assert len(queue) == CURRENT_RUN_SIZE


def test_all_fifty_current_run_records_receive_deterministic_evaluation(
    live_run,
):
    """
    The whole point. Evaluation is regex; it is not rationed by a spend
    limit.
    """
    records = current_run_records()
    airtable = FakeAirtable(records)
    live_run.setattr(rp, "list_airtable_records", airtable)
    counter = CallCounter(QUALIFYING_SIGNALS)
    live_run.setattr(rp, "get_openai_client", counter)

    queue = rp.fetch_ai_queue([record["id"] for record in records])
    prioritized = rp.prioritize_ai_queue(queue, set(), now=NOW)
    summary = rp.process_ai_queue(prioritized, now=NOW)

    assert len(prioritized) == CURRENT_RUN_SIZE
    assert summary.queue_size == CURRENT_RUN_SIZE
    assert summary.prefilter_rejected + summary.prefilter_accepted == (
        CURRENT_RUN_SIZE
    )


def test_the_batch_limit_no_longer_sizes_the_queue(live_run):
    """
    The exact defect: window = AI_BATCH_LIMIT * 5 made this 25.
    """
    records = current_run_records()
    live_run.setattr(rp, "list_airtable_records", FakeAirtable(records))

    for batch_limit in (1, 5, 20):
        live_run.setattr(rp, "AI_BATCH_LIMIT", batch_limit)
        queue = rp.fetch_ai_queue([record["id"] for record in records])

        assert len(queue) == CURRENT_RUN_SIZE, (
            f"AI_BATCH_LIMIT={batch_limit} changed how many records are "
            f"evaluated"
        )


def test_the_scan_limit_is_independent_of_the_batch_limit():
    """
    Not merely different numbers: the scan limit must not be *derived* from
    the spend limit. ``window = AI_BATCH_LIMIT * 5`` is the line that caused
    this, so the source is checked too.
    """
    assert rp.PREFILTER_SCAN_LIMIT >= CURRENT_RUN_SIZE

    import ast
    import inspect

    # The docstring explains the old defect by name, so compare code only.
    tree = ast.parse(inspect.getsource(rp.fetch_ai_queue))
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }

    assert "AI_BATCH_LIMIT" not in names, (
        "the spend limit is sizing the evaluation queue again"
    )
    assert "PREFILTER_SCAN_LIMIT" in names


# ---------------------------------------------------------------------------
# Test 3 and 4 -- the batch limit caps calls, and only calls
# ---------------------------------------------------------------------------

def test_ai_batch_limit_permits_at_most_five_openai_calls(live_run):
    """Fifty strong leads, five calls."""
    records = [
        make_record(f"recStrong{index:03d}", text=GOHIGHLEVEL_PROVIDER)
        for index in range(CURRENT_RUN_SIZE)
    ]
    live_run.setattr(rp, "list_airtable_records", FakeAirtable(records))
    counter = CallCounter(QUALIFYING_SIGNALS)
    live_run.setattr(rp, "get_openai_client", counter)

    queue = rp.fetch_ai_queue([record["id"] for record in records])
    summary = rp.process_ai_queue(
        rp.prioritize_ai_queue(queue, set(), now=NOW), now=NOW
    )

    assert counter.requests_made == LIVE_AI_BATCH_LIMIT
    assert summary.ai_processed == LIVE_AI_BATCH_LIMIT
    assert summary.queue_size == CURRENT_RUN_SIZE


def test_deterministic_rejects_do_not_consume_the_call_limit(live_run):
    """
    Forty-nine free rejections and one real lead: the lead still gets its
    call. Before this fix a batch of rejects could exhaust nothing, but a
    small window meant the lead was never fetched at all.
    """
    records = current_run_records(strong_at=49)
    live_run.setattr(rp, "list_airtable_records", FakeAirtable(records))
    counter = CallCounter(QUALIFYING_SIGNALS)
    live_run.setattr(rp, "get_openai_client", counter)

    queue = rp.fetch_ai_queue([record["id"] for record in records])
    summary = rp.process_ai_queue(
        rp.prioritize_ai_queue(queue, set(), now=NOW), now=NOW
    )

    assert counter.requests_made == 1
    assert summary.ai_processed == 1
    assert summary.prefilter_rejected == CURRENT_RUN_SIZE - 1


# ---------------------------------------------------------------------------
# Test 5 and 7 -- position and ordering cannot hide a lead
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("position", [0, 24, 25, 49])
def test_a_strong_lead_reaches_the_ai_from_any_position(live_run, position):
    """
    Index 25 and 49 are the ones that mattered: both fell outside the old
    window.
    """
    records = current_run_records(strong_at=position)
    live_run.setattr(rp, "list_airtable_records", FakeAirtable(records))
    counter = CallCounter(QUALIFYING_SIGNALS)
    live_run.setattr(rp, "get_openai_client", counter)

    queue = rp.fetch_ai_queue([record["id"] for record in records])
    summary = rp.process_ai_queue(
        rp.prioritize_ai_queue(queue, set(), now=NOW), now=NOW
    )

    assert counter.requests_made == 1, (
        f"a strong lead at index {position} never reached the model"
    )
    assert summary.ai_processed == 1


def test_arbitrary_airtable_order_cannot_hide_a_current_run_candidate(
    live_run,
):
    """
    Records are fetched by ID, so the order Airtable happens to return them
    in is irrelevant. Reversed, rotated, or shuffled, the same 50 are
    evaluated and the same lead is found.
    """
    records = current_run_records(strong_at=49)
    ids = [record["id"] for record in records]

    for ordering in (records, list(reversed(records)), records[25:] + records[:25]):
        airtable = FakeAirtable(ordering)
        live_run.setattr(rp, "list_airtable_records", airtable)
        counter = CallCounter(QUALIFYING_SIGNALS)
        live_run.setattr(rp, "get_openai_client", counter)

        queue = rp.fetch_ai_queue(ids)
        summary = rp.process_ai_queue(
            rp.prioritize_ai_queue(queue, set(), now=NOW), now=NOW
        )

        assert len(queue) == CURRENT_RUN_SIZE
        assert summary.ai_processed == 1


def test_current_run_records_are_fetched_by_id_not_by_page(live_run):
    records = current_run_records()
    airtable = FakeAirtable(records)
    live_run.setattr(rp, "list_airtable_records", airtable)

    rp.fetch_ai_queue([record["id"] for record in records])

    id_queries = [q for q in airtable.queries if "RECORD_ID()" in q]

    assert id_queries, "the current run was not fetched by record ID"
    assert all(
        rp.build_unprocessed_status_clause() in query for query in id_queries
    )


def test_the_by_id_fetch_is_chunked_for_large_runs(live_run):
    """Airtable formulas ride in a URL, so 50 IDs go in pieces."""
    records = current_run_records()
    airtable = FakeAirtable(records)
    live_run.setattr(rp, "list_airtable_records", airtable)

    rp.fetch_ai_queue([record["id"] for record in records])

    id_queries = [q for q in airtable.queries if "RECORD_ID()" in q]

    assert len(id_queries) >= 2
    for query in id_queries:
        assert query.count("RECORD_ID()") <= rp.RECORD_ID_QUERY_CHUNK


# ---------------------------------------------------------------------------
# Test 6 -- current run outranks the backlog
# ---------------------------------------------------------------------------

def test_current_run_records_outrank_historical_backlog(live_run):
    """
    A fresh lead beats an equally strong record that has been sitting in the
    table since last month.
    """
    fresh = make_record("recFresh", text=GOHIGHLEVEL_PROVIDER, days_old=1)
    old = make_record("recBacklog", text=GOHIGHLEVEL_PROVIDER, days_old=4)

    ordered = rp.prioritize_ai_queue([old, fresh], {"recFresh"}, now=NOW)

    assert [record["id"] for record, _ in ordered] == [
        "recFresh", "recBacklog",
    ]


def test_the_backlog_only_fills_what_the_current_run_leaves(live_run):
    records = current_run_records()
    backlog = [
        make_record(f"recOld{index:03d}", text=GENERAL_GROWTH_QUESTION,
                    days_old=3)
        for index in range(500)
    ]
    live_run.setattr(
        rp, "list_airtable_records", FakeAirtable(records, backlog=backlog)
    )
    live_run.setattr(rp, "PREFILTER_SCAN_LIMIT", 120)

    queue = rp.fetch_ai_queue([record["id"] for record in records])

    assert len(queue) == 120
    assert sum(1 for r in queue if r["id"].startswith("recRun")) == (
        CURRENT_RUN_SIZE
    )


def test_a_current_run_larger_than_the_scan_limit_is_still_fully_evaluated(
    live_run,
):
    """
    The scan limit bounds the backlog read. It never rations this run's own
    imports -- that is the whole lesson of the 25-of-50 defect.
    """
    records = current_run_records(size=300)
    live_run.setattr(rp, "list_airtable_records", FakeAirtable(records))
    live_run.setattr(rp, "PREFILTER_SCAN_LIMIT", 50)

    queue = rp.fetch_ai_queue([record["id"] for record in records])

    assert len(queue) == 300


# ---------------------------------------------------------------------------
# The live record that was never evaluated
# ---------------------------------------------------------------------------

def test_the_surface_restoration_lead_is_evaluated_and_reaches_the_ai(
    live_run,
):
    """
    The exact record the live run left blank: Request Signal = 1, Service
    Signal = 0, Prequalification = Reject, AI Output blank, AI Status blank.
    """
    records = current_run_records(size=CURRENT_RUN_SIZE)
    records[49] = make_record(
        "recRun049", text=SURFACE_RESTORATION_LEAD_GEN
    )
    live_run.setattr(rp, "list_airtable_records", FakeAirtable(records))
    counter = CallCounter(QUALIFYING_SIGNALS)
    live_run.setattr(rp, "get_openai_client", counter)

    queue = rp.fetch_ai_queue([record["id"] for record in records])
    summary = rp.process_ai_queue(
        rp.prioritize_ai_queue(queue, set(), now=NOW), now=NOW
    )

    assert len(queue) == CURRENT_RUN_SIZE
    assert counter.requests_made == 1
    assert summary.ai_processed == 1
