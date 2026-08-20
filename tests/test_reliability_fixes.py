"""
Regression tests for five reliability defects found reviewing PR #3.

1. GoHighLevel false negative. An explicit request for a named BruceTech
   service was hard-rejected as NO_BUSINESS_CONTEXT because the post said
   nothing about the company behind it. Thin context now routes to Manual
   Review instead of the bin.
2. Non-idempotent POST retries. Airtable record creation and the Apify
   run-start POST were replayed after ambiguous failures, which duplicates
   records and starts paid runs twice.
3. Dry-run coverage. Newly scraped posts never reached the prefilter during
   DRY_RUN, because nothing was created and the queue is fetched by record ID.
   A dry run reported on the backlog and said nothing about the scrape.
4. AI error state. A failed reprocess left an older Outreach Ready decision
   live, so a salesperson's view could still offer copy from a decision the
   pipeline could no longer reproduce.
5. OpenAI request budget. AI_BATCH_LIMIT counts records, not requests. With
   OPENAI_MAX_ATTEMPTS=3 a "limit" of 5 permitted 15 billed calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

import ai_retry
import intent
import run_pipeline as rp
from qualification import (
    REJECT_NO_BUSINESS_CONTEXT,
    TIER_MANUAL_REVIEW,
    TIER_REJECTED,
    evaluate_lead,
    prefilter_post,
)
from tests.fixtures import (
    GOHIGHLEVEL_EXPERT_REQUEST,
    GOHIGHLEVEL_PROVIDER,
    SOLO_MD_MARKETING_AGENCY,
)

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(days=1)).isoformat()

THIN_CONTEXT_SIGNALS = {
    "intent_strength": "strong",
    "service_categories": ["crm"],
    "problem_specificity": "specific",
    "business_context": "none",
    "purchase_signal": "ready_to_buy",
    "urgency": "medium",
    "business_impact": "medium",
    "location": "unknown",
    "buyer_role": "unknown",
    "resolved_status": "unresolved",
    "classification_confidence": 0.8,
    "disqualifier_codes": [],
}

DM = "Hi Oren, saw your GoHighLevel post. We build these at brucetech.ca. Chat?"
COMMENT = "Sounds like you need someone who knows the platform. I'll send a DM."


# ===========================================================================
# 1. GoHighLevel false negative
# ===========================================================================

def screen(text: str, *, days_old: float = 1):
    return prefilter_post(
        text,
        post_time=(NOW - timedelta(days=days_old)).isoformat(),
        now=NOW,
        max_post_age_days=5,
        comment_count=3,
    )


def decide(**kwargs):
    params = {
        "suggested_dm": DM,
        "suggested_comment": COMMENT,
        "recommended_channel": "direct_message",
        "now": NOW,
        **kwargs,
    }
    return evaluate_lead(THIN_CONTEXT_SIGNALS, **params)


def test_the_gohighlevel_post_is_an_explicit_service_request():
    result = screen(GOHIGHLEVEL_EXPERT_REQUEST)

    assert result.intent_type == intent.INTENT_PROVIDER_REQUEST
    assert result.match_basis == "named"
    assert rp.is_explicit_service_request(result) is True


def test_thin_context_no_longer_hard_rejects_an_explicit_request():
    decision = decide(
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        explicit_service_request=True,
    )

    assert REJECT_NO_BUSINESS_CONTEXT not in decision.hard_rejection_codes
    assert decision.tier != TIER_REJECTED


def test_thin_context_routes_to_manual_review_not_outreach():
    """Kept for a human. Never auto-contacted."""
    decision = decide(
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        explicit_service_request=True,
    )

    assert decision.tier == TIER_MANUAL_REVIEW
    assert decision.qualified is False
    assert decision.manual_review is True
    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""
    assert decision.suggested_comment == ""
    assert decision.recommended_channel == "do_not_contact"


def test_without_the_waiver_the_old_hard_rejection_still_applies():
    decision = decide(
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        explicit_service_request=False,
    )

    assert REJECT_NO_BUSINESS_CONTEXT in decision.hard_rejection_codes
    assert decision.tier == TIER_REJECTED


def test_real_business_context_is_completely_unaffected():
    """The waiver only ever applies when the model said context is none."""
    decision = evaluate_lead(
        {**THIN_CONTEXT_SIGNALS, "business_context": "small_business"},
        suggested_dm=DM,
        suggested_comment=COMMENT,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        explicit_service_request=True,
    )

    assert decision.qualified is True
    assert decision.outreach_ready is True
    assert decision.suggested_dm == DM


def test_the_waiver_needs_a_named_service_not_an_inferred_one():
    """
    An adjacent match is the model's call to make, so it does not earn the
    waiver. A marketing-agency request stays subject to NO_BUSINESS_CONTEXT.
    """
    adjacent = screen(SOLO_MD_MARKETING_AGENCY)

    assert adjacent.match_basis == "adjacent"
    assert adjacent.strong_service_match is False
    assert rp.is_explicit_service_request(adjacent) is False


#: A provider request whose service match is assembled from two weak
#: categories ("payment" + "system"), with no unambiguous term anywhere.
TWO_WEAK_CATEGORIES = (
    "Looking for someone to fix our payment system, the whole thing is a "
    "mess right now and I need it sorted properly."
)


def test_a_two_weak_category_match_does_not_earn_the_waiver():
    """
    The tightening. This post's basis is "named", because that value also
    covers weak corroboration, but no single term in it is unambiguous. A
    soft signal like this must not waive a hard rejection.
    """
    result = screen(TWO_WEAK_CATEGORIES)

    assert result.service_matched is True
    assert result.match_basis == "named"
    assert result.strong_service_match is False
    assert result.intent_type == intent.INTENT_PROVIDER_REQUEST

    # Both halves of the old rule are satisfied. The waiver still declines.
    assert rp.is_explicit_service_request(result) is False


def test_a_two_weak_category_match_without_context_is_still_rejected():
    """End to end: the record this tightening protects against."""
    result = screen(TWO_WEAK_CATEGORIES)
    decision = evaluate_lead(
        THIN_CONTEXT_SIGNALS,
        suggested_dm=DM,
        suggested_comment=COMMENT,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=result.intent_type,
        explicit_service_request=rp.is_explicit_service_request(result),
    )

    assert REJECT_NO_BUSINESS_CONTEXT in decision.hard_rejection_codes
    assert decision.tier == TIER_REJECTED
    assert decision.outreach_ready is False


def test_the_waiver_reads_the_strong_flag_not_the_basis():
    """
    Structural: match_basis is not a proxy for "the author named a service".
    A basis of "named" exists for both, so the two must be readable apart.
    """
    strong = screen(GOHIGHLEVEL_EXPERT_REQUEST)
    weak = screen(TWO_WEAK_CATEGORIES)

    assert strong.match_basis == weak.match_basis == "named"
    assert strong.strong_service_match is True
    assert weak.strong_service_match is False


def test_the_gohighlevel_example_reaches_manual_review_end_to_end():
    """
    The named example, from post text to Airtable payload: kept for a human,
    never auto-contacted.
    """
    result = screen(GOHIGHLEVEL_EXPERT_REQUEST)
    decision = evaluate_lead(
        THIN_CONTEXT_SIGNALS,
        suggested_dm=DM,
        suggested_comment=COMMENT,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=result.intent_type,
        explicit_service_request=rp.is_explicit_service_request(result),
    )
    payload = rp.map_decision_to_airtable(
        decision, THIN_CONTEXT_SIGNALS, prefilter=result
    )

    assert payload[rp.FIELD_LEAD_TIER] == TIER_MANUAL_REVIEW
    assert payload[rp.FIELD_QUALIFIED] is False
    assert payload[rp.FIELD_MANUAL_REVIEW] is True
    assert payload[rp.FIELD_OUTREACH_READY] is False
    assert payload[rp.FIELD_SUGGESTED_DM] == ""
    assert payload[rp.FIELD_SUGGESTED_COMMENT] == ""
    assert payload[rp.FIELD_RECOMMENDED_CHANNEL] == "do_not_contact"
    assert rp.FIELD_DISQUALIFIERS in payload
    assert "NO_BUSINESS_CONTEXT" not in payload[rp.FIELD_DISQUALIFIERS]


def test_a_strong_request_with_real_context_follows_normal_qualification():
    """
    The waiver is invisible whenever the model reported real context. This
    record qualifies and is outreach-ready exactly as it was before.
    """
    result = screen(GOHIGHLEVEL_EXPERT_REQUEST)
    decision = evaluate_lead(
        {**THIN_CONTEXT_SIGNALS, "business_context": "established_business"},
        suggested_dm=DM,
        suggested_comment=COMMENT,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=result.intent_type,
        explicit_service_request=rp.is_explicit_service_request(result),
    )

    assert decision.qualified is True
    assert decision.tier in {"Hot", "Qualified"}
    assert decision.manual_review is False
    assert decision.outreach_ready is True
    assert decision.suggested_dm == DM
    assert decision.recommended_channel == "direct_message"


def test_the_waiver_needs_a_request_intent():
    research = screen(
        "What CRM are other contractors using? Curious what everyone likes "
        "for tracking jobs and quotes these days."
    )

    assert research.intent_type == intent.INTENT_TOOL_RESEARCH
    assert rp.is_explicit_service_request(research) is False


def test_the_waiver_does_not_lift_any_other_hard_rejection():
    decision = evaluate_lead(
        {**THIN_CONTEXT_SIGNALS, "promotional_post": True},
        suggested_dm=DM,
        suggested_comment=COMMENT,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        explicit_service_request=True,
    )

    assert decision.tier == TIER_REJECTED
    assert decision.suggested_dm == ""


def test_a_strong_provider_request_with_context_still_reaches_outreach():
    """The healthy path must not have moved."""
    result = screen(GOHIGHLEVEL_PROVIDER)

    assert result.passed is True
    assert rp.is_explicit_service_request(result) is True


# ===========================================================================
# 2. Non-idempotent POST retries
# ===========================================================================

class FlakyTransport:
    """Fails the first call the way a lost response looks, then succeeds."""

    def __init__(self, failure: Exception | None = None, status: int = 500):
        self.calls = 0
        self.failure = failure
        self.status = status

    def __call__(self, method, url, **kwargs):
        self.calls += 1
        if self.calls == 1:
            if self.failure is not None:
                raise self.failure
            return _FakeResponse(self.status)
        return _FakeResponse(200)


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = ""

    def json(self):
        return {"records": [{"id": "recCreated"}]}

    def raise_for_status(self):
        raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(rp.time, "sleep", lambda *_: None)


@pytest.mark.parametrize(
    "failure",
    [
        requests.ReadTimeout("response never arrived"),
        requests.ConnectionError("connection reset mid-flight"),
    ],
)
def test_an_accepted_post_with_a_lost_response_is_not_resent(
    monkeypatch, failure
):
    """
    THE FAULT INJECTION. The server accepted the write; the response was
    lost. Replaying it would create a second record.
    """
    transport = FlakyTransport(failure=failure)
    monkeypatch.setattr(rp.requests, "request", transport)

    with pytest.raises(rp.AmbiguousWriteError):
        rp.request_with_retry("POST", "https://api.airtable.com/v0/app/tbl")

    assert transport.calls == 1, "the POST was submitted more than once"


def test_a_post_that_returns_500_is_not_resent(monkeypatch):
    """A 5xx is ambiguous: the write may already have been applied."""
    transport = FlakyTransport(status=500)
    monkeypatch.setattr(rp.requests, "request", transport)

    with pytest.raises(rp.AmbiguousWriteError):
        rp.request_with_retry("POST", "https://api.apify.com/v2/runs")

    assert transport.calls == 1


def test_a_post_that_never_reached_the_server_is_safe_to_retry(monkeypatch):
    """A connect timeout proves no bytes arrived, so a replay cannot duplicate."""
    transport = FlakyTransport(failure=requests.ConnectTimeout("no route"))
    monkeypatch.setattr(rp.requests, "request", transport)

    response = rp.request_with_retry("POST", "https://api.airtable.com/v0/x")

    assert response.status_code == 200
    assert transport.calls == 2


def test_a_rate_limited_post_is_safe_to_retry(monkeypatch):
    """429 means rejected before processing, not processed and lost."""
    transport = FlakyTransport(status=429)
    monkeypatch.setattr(rp.requests, "request", transport)

    response = rp.request_with_retry("POST", "https://api.airtable.com/v0/x")

    assert response.status_code == 200
    assert transport.calls == 2


@pytest.mark.parametrize("method", ["GET", "PATCH"])
def test_idempotent_methods_still_retry_normally(monkeypatch, method):
    """PATCH by record ID and GET are replayable; that behaviour is unchanged."""
    transport = FlakyTransport(failure=requests.ReadTimeout("lost"))
    monkeypatch.setattr(rp.requests, "request", transport)

    response = rp.request_with_retry(method, "https://api.airtable.com/v0/x")

    assert response.status_code == 200
    assert transport.calls == 2


def test_airtable_creation_reconciles_instead_of_replaying(monkeypatch):
    """
    An ambiguous create reads Airtable back and reports what landed. It does
    not resend, and it does not pretend the import succeeded.
    """
    posts = [
        {"url": "https://www.facebook.com/groups/1/posts/1", "text": "a"},
        {"url": "https://www.facebook.com/groups/1/posts/2", "text": "b"},
    ]

    monkeypatch.setattr(rp, "DRY_RUN", False)
    monkeypatch.setattr(rp, "airtable_url", lambda: "https://airtable/x")
    monkeypatch.setattr(
        rp,
        "create_table_records",
        lambda *a, **k: (_ for _ in ()).throw(
            rp.AmbiguousWriteError("response lost")
        ),
    )
    # One of the two actually landed.
    landed_keys = set(rp.identity_from_apify(posts[0]).keys())
    monkeypatch.setattr(
        rp, "fetch_existing_identity_keys", lambda: landed_keys
    )

    with pytest.raises(rp.AmbiguousWriteError) as caught:
        rp.create_new_posts_in_airtable(posts, apify_run_id="run1")

    assert "1 of 2 posts" in str(caught.value)
    assert "Nothing was retried" in str(caught.value)


def test_the_apify_start_post_cannot_be_replayed():
    """A duplicated start is a duplicated invoice."""
    assert "POST" not in rp.IDEMPOTENT_METHODS


# ===========================================================================
# 3. Dry-run coverage of newly scraped posts
# ===========================================================================

def test_new_posts_become_evaluable_records_in_a_dry_run():
    posts = [
        {
            "url": "https://www.facebook.com/groups/1/posts/99",
            "text": GOHIGHLEVEL_PROVIDER,
            "time": FRESH,
            "user": {"name": "Test Author"},
        }
    ]

    records = rp.build_dry_run_records(posts, apify_run_id="runX")

    assert len(records) == 1
    assert records[0]["id"].startswith(rp.DRY_RUN_RECORD_PREFIX)
    assert records[0]["fields"][rp.FIELD_TEXT] == GOHIGHLEVEL_PROVIDER


def test_a_dry_run_prefilters_and_qualifies_this_run_s_own_scrape(monkeypatch):
    """
    END TO END. A fresh strong lead that is not in Airtable reaches the
    prefilter and the model, while zero Airtable writes and zero Apify runs
    happen.
    """
    posts = [
        {
            "url": f"https://www.facebook.com/groups/1/posts/{index}",
            "text": GOHIGHLEVEL_PROVIDER,
            "time": FRESH,
            "user": {"name": "Test Author"},
        }
        for index in range(3)
    ]

    openai_calls: list = []
    http_calls: list = []

    monkeypatch.setattr(rp, "DRY_RUN", True)
    monkeypatch.setattr(rp, "MAX_POST_AGE_DAYS", 5)
    monkeypatch.setattr(rp, "AI_BATCH_LIMIT", 5)

    # The real update_airtable_records runs. Any HTTP it attempts is a bug,
    # so the transport records the attempt rather than being stubbed out.
    def no_http(*args, **kwargs):
        http_calls.append(args)
        raise AssertionError("DRY_RUN must not issue an HTTP request")

    monkeypatch.setattr(rp, "request_with_retry", no_http)

    def fake_extract(fields):
        openai_calls.append(fields)
        return {
            "intent_strength": "strong",
            "service_categories": ["crm"],
            "problem_specificity": "detailed",
            "business_context": "small_business",
            "purchase_signal": "ready_to_buy",
            "urgency": "high",
            "business_impact": "high",
            "location": "toronto_gta",
            "buyer_role": "owner_or_decision_maker",
            "resolved_status": "unresolved",
            "classification_confidence": 0.9,
            "disqualifier_codes": [],
            "lead_summary": "x",
            "evidence": "y",
            "service_match": "CRM",
            "suggested_dm": DM,
            "suggested_comment": COMMENT,
            "recommended_channel": "direct_message",
        }

    monkeypatch.setattr(rp, "extract_signals", fake_extract)

    records = rp.build_dry_run_records(posts, apify_run_id="runX")
    prioritized = rp.prioritize_ai_queue(
        records, {r["id"] for r in records}, now=NOW
    )
    summary = rp.process_ai_queue(prioritized, now=NOW)

    # They were evaluated, not skipped.
    assert summary.queue_size == 3
    assert summary.prefilter_accepted == 3
    assert summary.ai_processed == 3
    assert len(openai_calls) == 3

    # And nothing was written: the real write path short-circuited on
    # DRY_RUN before it could reach the transport.
    assert http_calls == []


def test_a_dry_run_writes_nothing_even_when_records_qualify(monkeypatch, capsys):
    monkeypatch.setattr(rp, "DRY_RUN", True)

    def explode(*args, **kwargs):
        raise AssertionError("DRY_RUN must not issue an HTTP request")

    monkeypatch.setattr(rp, "request_with_retry", explode)

    rp.update_airtable_records(
        [{"id": "rec1", "fields": {rp.FIELD_QUALIFIED: True}}]
    )
    created = rp.create_new_posts_in_airtable(
        [{"url": "https://www.facebook.com/groups/1/posts/1"}]
    )

    assert created == []
    assert "[DRY RUN]" in capsys.readouterr().out


def test_a_dry_run_starts_no_apify_run():
    """The guard is a conjunction, so DRY_RUN wins over the input."""
    import inspect

    source = inspect.getsource(rp.main)

    assert "start_new_run=APIFY_START_NEW_RUN and not DRY_RUN" in source


# ===========================================================================
# 4. AI error state must not leave stale outreach live
# ===========================================================================

def test_a_failed_attempt_withdraws_outreach_eligibility():
    payload = rp.build_error_payload("boom", attempts=1, transient=True)

    assert payload[rp.FIELD_OUTREACH_READY] is False
    assert payload[rp.FIELD_SUGGESTED_DM] == ""
    assert payload[rp.FIELD_SUGGESTED_COMMENT] == ""


def test_a_failed_attempt_preserves_the_analysis():
    """History is kept. Only the instruction to act is withdrawn."""
    payload = rp.build_error_payload("boom", attempts=1, transient=True)

    for preserved in (
        rp.FIELD_LEAD_SCORE,
        rp.FIELD_QUALIFIED,
        rp.FIELD_LEAD_TIER,
        rp.FIELD_LEAD_SUMMARY,
        rp.FIELD_EVIDENCE,
    ):
        assert preserved not in payload


def test_the_suspension_is_recorded_for_the_operator():
    payload = rp.build_error_payload("boom", attempts=2, transient=False)
    diagnostics = json.loads(payload[rp.FIELD_AI_OUTPUT])

    assert diagnostics["outreach_suspended"] is True
    assert diagnostics["last_error"] == "boom"
    assert diagnostics["qualification_version"] == rp.QUALIFICATION_VERSION


def test_a_previously_outreach_ready_record_cannot_be_actioned_after_a_failure():
    """
    The scenario: yesterday the record was Outreach Ready with a DM. Today's
    reprocess failed. The salesperson's Outreach Ready view must not show it.
    """
    yesterday = {
        rp.FIELD_OUTREACH_READY: True,
        rp.FIELD_SUGGESTED_DM: "Hi, saw your post about the CRM build.",
        rp.FIELD_SUGGESTED_COMMENT: "I'll send you a quick DM.",
        rp.FIELD_LEAD_SCORE: 88,
        rp.FIELD_QUALIFIED: True,
    }

    today = {**yesterday, **rp.build_error_payload("timeout", attempts=1,
                                                   transient=True)}

    assert today[rp.FIELD_OUTREACH_READY] is False
    assert today[rp.FIELD_SUGGESTED_DM] == ""
    assert today[rp.FIELD_SUGGESTED_COMMENT] == ""
    # The analysis survives for whoever reads the row.
    assert today[rp.FIELD_LEAD_SCORE] == 88
    assert today[rp.FIELD_QUALIFIED] is True


def test_the_suspension_is_configurable_and_off_by_default_in_the_store():
    """A bare RetryStore keeps the old narrow behaviour."""
    bare = ai_retry.RetryStore(output_field="AI Output")
    payload = bare.build_failure_fields(
        "boom", attempts=1, transient=True,
        status_field="AI Status", version="v",
    )

    assert set(payload) == {"AI Status", "AI Output"}


# ===========================================================================
# 5. OpenAI request budget
# ===========================================================================

class CountingOpenAI:
    """Counts responses.create() calls and fails them all."""

    def __init__(self, failures: int = 999):
        self.creates = 0
        self.failures = failures

    def __call__(self):
        return self

    @property
    def responses(self):
        return self

    def create(self, **kwargs):
        self.creates += 1
        if self.creates <= self.failures:
            raise RuntimeError("upstream 500")
        return _FakeOpenAIResponse()


class _FakeOpenAIResponse:
    status = "completed"
    output_text = json.dumps({"service_categories": ["crm"]})


@pytest.fixture(autouse=True)
def _reset_budget():
    rp.reset_openai_request_budget()
    yield
    rp.reset_openai_request_budget()


def test_the_record_limit_and_the_request_limit_are_different_settings():
    assert rp.AI_BATCH_LIMIT != rp.OPENAI_REQUEST_BUDGET
    assert rp.OPENAI_REQUEST_BUDGET == (
        rp.AI_BATCH_LIMIT * rp.OPENAI_MAX_ATTEMPTS
    )


def test_retries_are_counted_against_the_request_budget(monkeypatch):
    """
    One record, three attempts, three billed requests. AI_BATCH_LIMIT would
    have counted this as one.
    """
    client = CountingOpenAI()
    monkeypatch.setattr(rp, "get_openai_client", client)
    monkeypatch.setattr(rp, "OPENAI_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(rp, "OPENAI_REQUEST_BUDGET", 100)

    with pytest.raises(RuntimeError):
        rp.extract_signals({rp.FIELD_TEXT: "anything"})

    assert client.creates == 3
    assert rp.openai_requests_made() == 3


def test_the_budget_caps_total_requests_across_records(monkeypatch):
    """
    THE COUNT THAT MATTERS. Five records at three attempts each would be 15
    requests. A budget of 7 stops at 7.
    """
    client = CountingOpenAI()
    monkeypatch.setattr(rp, "get_openai_client", client)
    monkeypatch.setattr(rp, "OPENAI_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(rp, "OPENAI_REQUEST_BUDGET", 7)

    for _ in range(5):
        try:
            rp.extract_signals({rp.FIELD_TEXT: "anything"})
        except rp.OpenAIBudgetExhausted:
            break
        except RuntimeError:
            continue

    assert client.creates == 7
    assert rp.openai_requests_made() == 7


def test_the_queue_stops_when_the_budget_is_exhausted(monkeypatch):
    client = CountingOpenAI()
    monkeypatch.setattr(rp, "get_openai_client", client)
    monkeypatch.setattr(rp, "OPENAI_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(rp, "OPENAI_REQUEST_BUDGET", 3)
    monkeypatch.setattr(rp, "AI_BATCH_LIMIT", 10)
    monkeypatch.setattr(rp, "MAX_POST_AGE_DAYS", 5)
    monkeypatch.setattr(rp, "update_airtable_records", lambda batch: None)
    monkeypatch.setattr(rp, "mark_ai_error", lambda *a, **k: None)

    records = [
        {
            "id": f"rec{index}",
            "fields": {
                rp.FIELD_TEXT: GOHIGHLEVEL_PROVIDER,
                rp.FIELD_TIME: FRESH,
                rp.FIELD_URL: f"https://www.facebook.com/groups/1/posts/{index}",
            },
        }
        for index in range(6)
    ]

    summary = rp.process_ai_queue(
        rp.prioritize_ai_queue(records, set(), now=NOW), now=NOW
    )

    assert summary.budget_exhausted is True
    assert client.creates == 3
    assert summary.ai_processed < 6


def test_a_zero_budget_disables_the_ceiling(monkeypatch):
    monkeypatch.setattr(rp, "OPENAI_REQUEST_BUDGET", 0)

    for _ in range(50):
        rp.consume_openai_request()

    assert rp.openai_requests_made() == 50


def test_the_budget_is_reported_in_the_run_summary(monkeypatch):
    monkeypatch.setattr(rp, "OPENAI_REQUEST_BUDGET", 5)
    rp.consume_openai_request()

    summary = rp.RunSummary()
    summary.budget_exhausted = True

    rendered = summary.render()

    assert "OpenAI requests" in rendered
    assert "BUDGET EXHAUSTED" in rendered


# ===========================================================================
# Workflow configuration: both limits must actually reach the pipeline
# ===========================================================================

WORKFLOW = ".github/workflows/facebook-leads.yml"


@pytest.fixture(scope="module")
def workflow():
    yaml = pytest.importorskip("yaml")
    with open(WORKFLOW, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _dispatch_inputs(workflow):
    # PyYAML parses the bare key `on:` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    return triggers["workflow_dispatch"]["inputs"]


def _pipeline_env(workflow):
    steps = workflow["jobs"]["run-pipeline"]["steps"]
    return next(step for step in steps if "env" in step)["env"]


def test_the_workflow_offers_both_limits_as_inputs(workflow):
    inputs = _dispatch_inputs(workflow)

    assert "ai_batch_limit" in inputs
    assert "openai_request_budget" in inputs


def test_the_record_limit_is_described_as_records(workflow):
    """It was described as "Max AI calls", which is what caused the confusion."""
    description = _dispatch_inputs(workflow)["ai_batch_limit"]["description"]

    assert "records" in description.lower()
    assert "call" not in description.lower()


def test_the_request_budget_input_says_retries_are_counted(workflow):
    description = _dispatch_inputs(workflow)["openai_request_budget"][
        "description"
    ]

    assert "retries" in description.lower()
    assert "request" in description.lower()


def test_the_workflow_passes_both_limits_to_the_pipeline(workflow):
    env = _pipeline_env(workflow)

    assert "AI_BATCH_LIMIT" in env
    assert "OPENAI_REQUEST_BUDGET" in env
    assert "ai_batch_limit" in env["AI_BATCH_LIMIT"]
    assert "openai_request_budget" in env["OPENAI_REQUEST_BUDGET"]


def test_an_empty_budget_input_falls_back_to_the_derived_default(monkeypatch):
    """
    The workflow passes "" when the operator leaves the box blank. env_int
    must treat that as unset so the derived default applies rather than 0,
    which would disable the ceiling.
    """
    monkeypatch.setenv("OPENAI_REQUEST_BUDGET", "")

    assert rp.env_int("OPENAI_REQUEST_BUDGET", 60) == 60


def test_an_explicit_zero_disables_the_ceiling(monkeypatch):
    monkeypatch.setenv("OPENAI_REQUEST_BUDGET", "0")

    assert rp.env_int("OPENAI_REQUEST_BUDGET", 60) == 0


README = "README.md"


def _readme_normal_run_row() -> str:
    with open(README, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("| Normal daily run "):
                return line
    raise AssertionError("README has no 'Normal daily run' recommendation row")


def test_the_workflow_default_matches_the_application_default(workflow):
    """
    Three places name this number: the dispatch input, the Python default,
    and the README recommendation. They drifted once (workflow 40 vs
    application 20), so they are asserted equal rather than trusted.
    """
    workflow_default = _dispatch_inputs(workflow)["ai_batch_limit"]["default"]

    assert int(workflow_default) == rp.DEFAULT_AI_BATCH_LIMIT


def test_the_workflow_env_fallback_matches_the_application_default(workflow):
    """The `|| '20'` fallback must not drift from the input default either."""
    expression = _pipeline_env(workflow)["AI_BATCH_LIMIT"]

    assert f"'{rp.DEFAULT_AI_BATCH_LIMIT}'" in expression


def test_the_readme_normal_run_matches_the_application_default():
    row = _readme_normal_run_row()

    assert f"`{rp.DEFAULT_AI_BATCH_LIMIT}`" in row


def test_the_readme_normal_run_states_the_derived_budget():
    """
    20 records at 3 attempts is 60 requests. If either default moves, this
    row is wrong and the test says so.
    """
    derived = rp.DEFAULT_AI_BATCH_LIMIT * rp.DEFAULT_OPENAI_MAX_ATTEMPTS
    row = _readme_normal_run_row()

    assert derived == 60
    assert f"derives {derived}" in row
    assert f"{derived} requests" in row
    assert "blank" in row.lower()


def test_the_derived_budget_is_what_the_pipeline_actually_computes(monkeypatch):
    """The README's arithmetic is the code's arithmetic, not a coincidence."""
    monkeypatch.delenv("OPENAI_REQUEST_BUDGET", raising=False)

    derived = rp.env_int(
        "OPENAI_REQUEST_BUDGET",
        rp.DEFAULT_AI_BATCH_LIMIT * rp.DEFAULT_OPENAI_MAX_ATTEMPTS,
    )

    assert derived == 60


def test_the_readme_keeps_the_testing_and_backlog_recommendations():
    """The lower testing row and the explicit backlog override must survive."""
    with open(README, encoding="utf-8") as handle:
        readme = handle.read()

    assert "| Testing a change | `5` | leave blank (derives 15) |" in readme
    assert "| Working a backlog | `100` | `150` |" in readme


def test_the_pipeline_still_runs_the_real_entry_point(workflow):
    """Guards against leaving a temporary smoke-test step behind again."""
    steps = workflow["jobs"]["run-pipeline"]["steps"]
    commands = [step.get("run", "") for step in steps]

    assert any("python run_pipeline.py" in command for command in commands)


def test_the_schedule_stays_disabled(workflow):
    """This branch must not re-enable the daily run."""
    triggers = workflow.get("on", workflow.get(True))

    assert "schedule" not in triggers


# ===========================================================================
# A failed reprocess also withdraws the recommended channel
# ===========================================================================

def test_a_failed_attempt_resets_the_recommended_channel():
    """
    Outreach Ready is the gate the pipeline enforces, but nothing downstream
    of Airtable is enforced by this code. A leftover "direct_message" on a
    superseded decision reads as an instruction to whoever opens the row.
    """
    payload = rp.build_error_payload("boom", attempts=1, transient=True)

    assert payload[rp.FIELD_RECOMMENDED_CHANNEL] == "do_not_contact"


def test_the_whole_call_to_action_is_withdrawn_together():
    payload = rp.build_error_payload("boom", attempts=1, transient=True)

    assert payload[rp.FIELD_OUTREACH_READY] is False
    assert payload[rp.FIELD_SUGGESTED_DM] == ""
    assert payload[rp.FIELD_SUGGESTED_COMMENT] == ""
    assert payload[rp.FIELD_RECOMMENDED_CHANNEL] == "do_not_contact"


def test_the_channel_reset_is_opt_in_on_the_store():
    bare = ai_retry.RetryStore(output_field="AI Output")
    payload = bare.build_failure_fields(
        "boom", attempts=1, transient=True,
        status_field="AI Status", version="v",
    )

    assert "Recommended channel" not in payload
