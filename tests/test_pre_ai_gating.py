"""
Regression tests for the pre-AI gating defect found in the first production
run of PR #3 (GitHub Actions run 32198160409, 2026-08-18).

WHAT HAPPENED

The run read Apify run dLa1GU9ahR0LubuKc, whose 768 posts were already in
Airtable. Six records carried Prequalification = "Send to AI". The pipeline
read that Airtable *formula* field as a human's selection, so those six
skipped the deterministic prefilter entirely. Five of them fitted inside
AI_BATCH_LIMIT and were sent to gpt-5-mini. Every one was dated between
2026-07-24 and 2026-08-01 -- 17 to 25 days old against MAX_POST_AGE_DAYS=5 --
and every one came back hard-rejected with STALE_POST, after the call had
been paid for.

THE TWO RULES THESE TESTS PIN

1. Prequalification is machine generated. It is never human approval, and it
   overrides nothing. Human Decision = "Approve" is the only human override.
2. The maximum-age check runs before the AI queue and before the OpenAI
   client is constructed, and nothing lifts it -- not an Approve, not a
   formula value, not ENFORCE_PYTHON_PREFILTER=false.

Every test that asserts "no OpenAI call" counts calls to
``get_openai_client``, which ``extract_signals`` must go through to reach the
API. A count of zero means the client was never even built.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import intent
import run_pipeline as rp
from qualification import (
    HUMAN_OVERRIDABLE_HARD_REJECTIONS,
    NON_OVERRIDABLE_HARD_REJECTIONS,
    REJECT_ALREADY_RESOLVED,
    REJECT_HUMAN_REJECTED,
    REJECT_NO_BUYING_INTENT,
    REJECT_NO_SERVICE_MATCH,
    REJECT_PROMOTIONAL_POST,
    REJECT_STALE_POST,
    TIER_REJECTED,
    collect_hard_rejections,
    evaluate_lead,
    prefilter_post,
)
from tests.fixtures import (
    AIRBNB_TENANCY_ADVICE,
    GOHIGHLEVEL_PROVIDER,
    PROMOTIONAL_AGENCY,
    WEBSITE_WORTH_IT_ADVICE,
)

#: The date of the production run this file exists because of.
NOW = datetime(2026, 8, 18, 23, 41, 0, tzinfo=timezone.utc)

#: One of the five posts the run actually paid for: 2026-08-01, 17 days old.
STALE_DAYS = 17

MAX_AGE = 5


# ---------------------------------------------------------------------------
# Harness
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


PROVIDER_SIGNALS = {
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
    "evidence": "Asks for the whole GoHighLevel setup.",
    "service_match": "CRM",
    "suggested_dm": (
        "Hi, saw your post about the GoHighLevel setup for your HVAC team. "
        "We do exactly this at brucetech.ca. Worth a quick call?"
    ),
    "recommended_channel": "direct_message",
}

#: The signals gpt-5-mini actually returned for the Airbnb post: a 77-point
#: score built entirely out of services the author never asked for.
SOLUTION_HOPPED_SIGNALS = {
    **PROVIDER_SIGNALS,
    "intent_strength": "moderate",
    "purchase_signal": "researching",
    "urgency": "medium",
    "lead_summary": "Rental host could automate tenant screening.",
    "evidence": "Mentions background checks and booking management.",
    "suggested_dm": (
        "Hi, saw your post about tenant screening. We build automation "
        "workflows and CRM integrations at brucetech.ca. Worth a chat?"
    ),
}


def make_record(
    record_id: str,
    *,
    text: str,
    days_old: float,
    prequalification: str | None = None,
    human_decision: str | None = None,
) -> dict:
    fields = {
        rp.FIELD_TEXT: text,
        rp.FIELD_TIME: (NOW - timedelta(days=days_old)).isoformat(),
        rp.FIELD_URL: f"https://www.facebook.com/groups/1/posts/{record_id}",
        rp.FIELD_USER_NAME: "Test Author",
        rp.FIELD_COMMENTS: 3,
    }

    if prequalification is not None:
        fields[rp.FIELD_PREQUALIFICATION] = prequalification
    if human_decision is not None:
        fields[rp.FIELD_HUMAN_DECISION] = human_decision

    return {"id": record_id, "fields": fields}


def run_queue(monkeypatch, records, *, response_json=None, max_age=MAX_AGE):
    """
    Put records through the real queue path with a counting fake OpenAI.

    Returns (summary, updates, counter).
    """
    counter = CallCounter(response_json)
    updates: list[dict] = []

    monkeypatch.setattr(rp, "MAX_POST_AGE_DAYS", max_age)
    monkeypatch.setattr(rp, "get_openai_client", counter)
    monkeypatch.setattr(
        rp, "update_airtable_records", lambda batch: updates.extend(batch)
    )

    prioritized = rp.prioritize_ai_queue(records, set(), now=NOW)
    summary = rp.process_ai_queue(prioritized, now=NOW)

    return summary, updates, counter


def fields_for(updates: list[dict], record_id: str) -> dict:
    for update in updates:
        if update.get("id") == record_id:
            return update.get("fields", {}) or {}

    raise AssertionError(f"No Airtable update was written for {record_id}.")


# ---------------------------------------------------------------------------
# Required test 1 -- stale + formula prequalified + no human decision
#
# This is the production defect, reproduced exactly.
# ---------------------------------------------------------------------------

def test_stale_formula_prequalified_record_never_reaches_openai(monkeypatch):
    record = make_record(
        "recStale",
        text=GOHIGHLEVEL_PROVIDER,
        days_old=STALE_DAYS,
        prequalification="Send to AI",
    )

    summary, updates, counter = run_queue(monkeypatch, [record])

    assert counter.clients_built == 0
    assert counter.requests_made == 0
    assert summary.ai_processed == 0
    assert summary.ai_errors == 0
    assert summary.stale_skipped == 1

    written = fields_for(updates, "recStale")
    assert REJECT_STALE_POST in written[rp.FIELD_DISQUALIFIERS]
    assert written[rp.FIELD_QUALIFIED] is False
    assert written[rp.FIELD_OUTREACH_READY] is False
    assert written[rp.FIELD_LEAD_TIER] == TIER_REJECTED
    assert written[rp.FIELD_SUGGESTED_DM] == ""


def test_the_prefilter_reports_stale_as_a_machine_readable_code():
    result = prefilter_post(
        GOHIGHLEVEL_PROVIDER,
        post_time=(NOW - timedelta(days=STALE_DAYS)).isoformat(),
        now=NOW,
        max_post_age_days=MAX_AGE,
    )

    assert result.passed is False
    assert result.is_stale is True
    assert result.rejection_codes == [REJECT_STALE_POST]
    assert result.blocks_ai_call is True


def test_the_stale_gate_holds_with_the_python_prefilter_disabled(monkeypatch):
    """
    ENFORCE_PYTHON_PREFILTER turns off the *heuristic* prefilter. The age
    check is not a heuristic and is not covered by that switch.
    """
    monkeypatch.setattr(rp, "ENFORCE_PYTHON_PREFILTER", False)

    record = make_record(
        "recStaleNoEnforce",
        text=GOHIGHLEVEL_PROVIDER,
        days_old=STALE_DAYS,
        prequalification="Send to AI",
    )

    summary, _, counter = run_queue(monkeypatch, [record])

    assert counter.clients_built == 0
    assert summary.ai_processed == 0
    assert summary.stale_skipped == 1


def test_every_post_in_the_august_5_dataset_age_range_is_blocked(monkeypatch):
    """
    The five posts the production run paid for, by their real dates.

    Re-running that dataset must now cost nothing.
    """
    post_dates = [
        "2026-07-24",
        "2026-07-31",
        "2026-08-01",
        "2026-08-01",
        "2026-07-28",
    ]

    records = [
        {
            "id": f"recLive{index}",
            "fields": {
                rp.FIELD_TEXT: GOHIGHLEVEL_PROVIDER,
                rp.FIELD_TIME: f"{date}T12:00:00.000Z",
                rp.FIELD_URL: f"https://www.facebook.com/groups/1/posts/{index}",
                rp.FIELD_PREQUALIFICATION: "Send to AI",
            },
        }
        for index, date in enumerate(post_dates)
    ]

    summary, _, counter = run_queue(monkeypatch, records)

    assert counter.clients_built == 0
    assert summary.ai_processed == 0
    assert summary.stale_skipped == 5
    assert summary.rejected == 5


# ---------------------------------------------------------------------------
# Required test 2 -- Prequalification is not a human override
# ---------------------------------------------------------------------------

def test_prequalification_send_to_ai_is_not_a_human_override():
    fields = {rp.FIELD_PREQUALIFICATION: "Send to AI"}

    assert rp.is_airtable_prequalified(fields) is True
    assert rp.is_human_approved(fields) is False


def test_prequalification_does_not_bypass_the_prefilter(monkeypatch):
    """
    A fresh post the intent heuristic vetoes, carrying the formula value and
    no human decision, must still be prefiltered out.
    """
    record = make_record(
        "recFormulaOnly",
        text=WEBSITE_WORTH_IT_ADVICE,
        days_old=1,
        prequalification="Send to AI",
    )

    summary, updates, counter = run_queue(monkeypatch, [record])

    assert counter.clients_built == 0
    assert summary.ai_processed == 0
    assert summary.prefilter_rejected == 1
    assert fields_for(updates, "recFormulaOnly")[rp.FIELD_QUALIFIED] is False


def test_the_pipeline_passes_no_override_for_a_formula_only_record(monkeypatch):
    """The wiring: what qualify_post is actually told about the record."""
    captured: dict = {}

    def fake_qualify(fields, **kwargs):
        captured.update(kwargs)
        return (
            evaluate_lead(PROVIDER_SIGNALS, now=NOW),
            PROVIDER_SIGNALS,
        )

    monkeypatch.setattr(rp, "qualify_post", fake_qualify)

    record = make_record(
        "recFresh",
        text=GOHIGHLEVEL_PROVIDER,
        days_old=1,
        prequalification="Send to AI",
    )
    run_queue(monkeypatch, [record])

    assert captured["human_override"] is False


# ---------------------------------------------------------------------------
# Required test 3 -- Approve is the only source of the override
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fields, expected",
    [
        ({rp.FIELD_HUMAN_DECISION: "Approve"}, True),
        ({rp.FIELD_HUMAN_DECISION: "approve"}, True),
        ({rp.FIELD_HUMAN_DECISION: " Approve "}, True),
        ({rp.FIELD_HUMAN_DECISION: "Review"}, False),
        ({rp.FIELD_HUMAN_DECISION: "Reject"}, False),
        ({rp.FIELD_HUMAN_DECISION: ""}, False),
        ({rp.FIELD_PREQUALIFICATION: "Send to AI"}, False),
        (
            {
                rp.FIELD_PREQUALIFICATION: "Send to AI",
                rp.FIELD_HUMAN_DECISION: "Review",
            },
            False,
        ),
        ({}, False),
    ],
)
def test_only_human_decision_approve_enables_the_override(fields, expected):
    assert rp.is_human_approved(fields) is expected


def test_the_queue_fetches_human_approved_records_in_their_own_phase(
    monkeypatch,
):
    """
    An override nobody fetches is not an override. Approved records get a
    dedicated query so they cannot fall off the end of the backlog window.
    """
    queries: list[str] = []

    def fake_list(*, formula, fields, **kwargs):
        queries.append(formula)
        return []

    monkeypatch.setattr(rp, "list_airtable_records", fake_list)

    rp.fetch_ai_queue()

    assert rp.FIELD_HUMAN_DECISION in queries[0]
    assert "Approve" in queries[0]


def test_the_approved_phase_ignores_require_airtable_prequalification(
    monkeypatch,
):
    """A machine narrowing flag does not get to veto a person."""
    monkeypatch.setattr(rp, "REQUIRE_AIRTABLE_PREQUALIFICATION", True)

    queries: list[str] = []

    def fake_list(*, formula, fields, **kwargs):
        queries.append(formula)
        return []

    monkeypatch.setattr(rp, "list_airtable_records", fake_list)

    rp.fetch_ai_queue()

    assert any(
        "Approve" in query and rp.FIELD_HUMAN_DECISION in query
        for query in queries
    )


def test_the_queue_does_not_fetch_a_record_twice(monkeypatch):
    """A record that is both approved and formula-marked appears once."""
    shared = make_record("recBoth", text=GOHIGHLEVEL_PROVIDER, days_old=1,
                         human_decision="Approve",
                         prequalification="Send to AI")

    def fake_list(*, formula, fields, **kwargs):
        if rp.FIELD_HUMAN_DECISION in formula:
            return [shared]
        if rp.FIELD_PREQUALIFICATION in formula:
            return [shared]
        return []

    monkeypatch.setattr(rp, "list_airtable_records", fake_list)

    records = rp.fetch_ai_queue()

    assert [record["id"] for record in records] == ["recBoth"]


def test_approve_is_what_lets_a_vetoed_record_reach_the_model(monkeypatch):
    """
    The override has to actually do something, or these tests would pass on a
    pipeline that simply never calls the AI.
    """
    record = make_record(
        "recApproved",
        text=WEBSITE_WORTH_IT_ADVICE,
        days_old=1,
        human_decision="Approve",
    )

    summary, _, counter = run_queue(
        monkeypatch, [record], response_json=PROVIDER_SIGNALS
    )

    assert counter.clients_built == 1
    assert counter.requests_made == 1
    assert summary.ai_processed == 1


def test_review_does_not_bypass_deterministic_filtering(monkeypatch):
    record = make_record(
        "recReview",
        text=WEBSITE_WORTH_IT_ADVICE,
        days_old=1,
        human_decision="Review",
    )

    summary, _, counter = run_queue(monkeypatch, [record])

    assert counter.clients_built == 0
    assert summary.ai_processed == 0
    assert summary.prefilter_rejected == 1


# ---------------------------------------------------------------------------
# Required test 4 -- Approve plus stale is still no AI call
# ---------------------------------------------------------------------------

def test_approve_cannot_spend_a_call_on_a_stale_post(monkeypatch):
    record = make_record(
        "recApprovedStale",
        text=GOHIGHLEVEL_PROVIDER,
        days_old=STALE_DAYS,
        human_decision="Approve",
        prequalification="Send to AI",
    )

    summary, updates, counter = run_queue(monkeypatch, [record])

    assert counter.clients_built == 0
    assert counter.requests_made == 0
    assert summary.ai_processed == 0
    assert summary.stale_skipped == 1

    written = fields_for(updates, "recApprovedStale")
    assert REJECT_STALE_POST in written[rp.FIELD_DISQUALIFIERS]
    assert written[rp.FIELD_QUALIFIED] is False


def test_stale_post_survives_the_override_at_decision_time():
    """
    Belt and braces: even if a stale record somehow reached evaluate_lead,
    the override cannot remove STALE_POST.
    """
    decision = evaluate_lead(
        PROVIDER_SIGNALS,
        suggested_dm=PROVIDER_SIGNALS["suggested_dm"],
        recommended_channel="direct_message",
        post_time=(NOW - timedelta(days=STALE_DAYS)).isoformat(),
        now=NOW,
        max_post_age_days=MAX_AGE,
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        human_override=True,
    )

    assert REJECT_STALE_POST in decision.hard_rejection_codes
    assert decision.qualified is False
    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""


def test_stale_is_declared_non_overridable():
    assert REJECT_STALE_POST in NON_OVERRIDABLE_HARD_REJECTIONS
    assert REJECT_STALE_POST not in HUMAN_OVERRIDABLE_HARD_REJECTIONS


# ---------------------------------------------------------------------------
# Required test 5 -- Approve does not launder a promotional seller
# ---------------------------------------------------------------------------

def test_approve_does_not_rescue_a_promotional_seller(monkeypatch):
    record = make_record(
        "recPromo",
        text=PROMOTIONAL_AGENCY,
        days_old=1,
        human_decision="Approve",
    )

    summary, updates, counter = run_queue(monkeypatch, [record])

    assert counter.clients_built == 0
    assert summary.ai_processed == 0

    written = fields_for(updates, "recPromo")
    assert REJECT_PROMOTIONAL_POST in written[rp.FIELD_DISQUALIFIERS]
    assert written[rp.FIELD_QUALIFIED] is False


def test_approve_does_not_lift_any_non_overridable_code():
    """The whole set, not just the ones with a fixture."""
    for code in sorted(NON_OVERRIDABLE_HARD_REJECTIONS):
        codes = collect_hard_rejections(
            PROVIDER_SIGNALS,
            extra_codes=[code],
            human_override=True,
        )

        assert code in codes, f"{code} was lifted by a human override"


def test_approve_lifts_exactly_the_intent_heuristics():
    codes = collect_hard_rejections(
        PROVIDER_SIGNALS,
        intent_type=intent.INTENT_UNRELATED,
        human_override=True,
    )

    assert REJECT_NO_BUYING_INTENT not in codes

    without_override = collect_hard_rejections(
        PROVIDER_SIGNALS,
        intent_type=intent.INTENT_UNRELATED,
        human_override=False,
    )

    assert REJECT_NO_BUYING_INTENT in without_override


def test_the_two_override_sets_partition_every_rejection_code():
    from qualification import HARD_REJECTION_REASONS

    assert HUMAN_OVERRIDABLE_HARD_REJECTIONS.isdisjoint(
        NON_OVERRIDABLE_HARD_REJECTIONS
    )
    assert (
        HUMAN_OVERRIDABLE_HARD_REJECTIONS | NON_OVERRIDABLE_HARD_REJECTIONS
    ) == set(HARD_REJECTION_REASONS)


def test_approve_does_not_rescue_an_already_resolved_request(monkeypatch):
    from tests.fixtures import RESOLVED_REQUEST

    record = make_record(
        "recResolved",
        text=RESOLVED_REQUEST,
        days_old=1,
        human_decision="Approve",
    )

    summary, updates, counter = run_queue(monkeypatch, [record])

    assert counter.clients_built == 0
    assert summary.ai_processed == 0
    assert REJECT_ALREADY_RESOLVED in fields_for(
        updates, "recResolved"
    )[rp.FIELD_DISQUALIFIERS]


# ---------------------------------------------------------------------------
# Required test 6 -- Human Decision = Reject
# ---------------------------------------------------------------------------

def test_human_reject_never_reaches_openai(monkeypatch):
    """A record someone turned down cannot be argued out of by the model."""
    record = make_record(
        "recRejected",
        text=GOHIGHLEVEL_PROVIDER,
        days_old=1,
        human_decision="Reject",
        prequalification="Send to AI",
    )

    summary, updates, counter = run_queue(monkeypatch, [record])

    assert counter.clients_built == 0
    assert counter.requests_made == 0
    assert summary.ai_processed == 0
    assert summary.human_rejected == 1

    written = fields_for(updates, "recRejected")
    assert REJECT_HUMAN_REJECTED in written[rp.FIELD_DISQUALIFIERS]
    assert written[rp.FIELD_QUALIFIED] is False
    assert written[rp.FIELD_OUTREACH_READY] is False
    assert written[rp.FIELD_SUGGESTED_DM] == ""


def test_human_reject_beats_a_fresh_strong_provider_request(monkeypatch):
    """Even the best possible post. The human decision is final."""
    record = make_record(
        "recRejectedStrong",
        text=GOHIGHLEVEL_PROVIDER,
        days_old=0.5,
        human_decision="Reject",
    )

    summary, _, counter = run_queue(
        monkeypatch, [record], response_json=PROVIDER_SIGNALS
    )

    assert counter.requests_made == 0
    assert summary.ai_processed == 0


# ---------------------------------------------------------------------------
# Required test 7 -- the solution-hopping regression from the live run
# ---------------------------------------------------------------------------

def test_airbnb_tenancy_question_is_not_a_brucetech_request():
    result = intent.classify_intent(AIRBNB_TENANCY_ADVICE)

    assert result.intent_type in {
        intent.INTENT_GENERAL_ADVICE,
        intent.INTENT_TOOL_RESEARCH,
        intent.INTENT_UNRELATED,
    }
    assert intent.match_services(AIRBNB_TENANCY_ADVICE).matched is False


def test_airbnb_tenancy_question_never_reaches_the_model(monkeypatch):
    """Fresh, and still not worth a token: nothing in it is a BruceTech job."""
    record = make_record(
        "recAirbnb",
        text=AIRBNB_TENANCY_ADVICE,
        days_old=1,
        prequalification="Send to AI",
    )

    summary, updates, counter = run_queue(monkeypatch, [record])

    assert counter.clients_built == 0
    assert summary.ai_processed == 0

    written = fields_for(updates, "recAirbnb")
    assert REJECT_NO_SERVICE_MATCH in written[rp.FIELD_DISQUALIFIERS]
    assert written[rp.FIELD_QUALIFIED] is False
    assert written[rp.FIELD_OUTREACH_READY] is False
    assert written[rp.FIELD_SUGGESTED_DM] == ""


def test_a_human_approve_cannot_solution_hop_the_airbnb_post(monkeypatch):
    """
    NO_SERVICE_MATCH is non-overridable, so approving it changes nothing.
    """
    record = make_record(
        "recAirbnbApproved",
        text=AIRBNB_TENANCY_ADVICE,
        days_old=1,
        human_decision="Approve",
    )

    summary, _, counter = run_queue(monkeypatch, [record])

    assert counter.clients_built == 0
    assert summary.ai_processed == 0


def test_invented_automation_signals_do_not_make_it_outreach_ready():
    """
    The 77-point score the model gave this post, fed straight into the
    decision. The author asked about tenancy law; a CRM is not an answer.
    """
    decision = evaluate_lead(
        SOLUTION_HOPPED_SIGNALS,
        suggested_dm=SOLUTION_HOPPED_SIGNALS["suggested_dm"],
        recommended_channel="direct_message",
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
        max_post_age_days=MAX_AGE,
        intent_type=intent.classify_intent(AIRBNB_TENANCY_ADVICE).intent_type,
        post_text=AIRBNB_TENANCY_ADVICE,
    )

    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""
    assert decision.recommended_channel == "do_not_contact"


def test_the_same_invented_signals_with_an_approve_still_do_not_contact():
    decision = evaluate_lead(
        SOLUTION_HOPPED_SIGNALS,
        suggested_dm=SOLUTION_HOPPED_SIGNALS["suggested_dm"],
        recommended_channel="direct_message",
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
        max_post_age_days=MAX_AGE,
        intent_type=intent.classify_intent(AIRBNB_TENANCY_ADVICE).intent_type,
        post_text=AIRBNB_TENANCY_ADVICE,
        human_override=True,
    )

    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""


# ---------------------------------------------------------------------------
# Required test 8 -- a fresh, legitimate provider request still gets through
#
# The gate has to let real leads past, or it is just an expensive way to
# process nothing.
# ---------------------------------------------------------------------------

def test_a_fresh_provider_request_still_reaches_openai(monkeypatch):
    record = make_record(
        "recGoHighLevel",
        text=GOHIGHLEVEL_PROVIDER,
        days_old=1,
        prequalification="Send to AI",
    )

    summary, updates, counter = run_queue(
        monkeypatch, [record], response_json=PROVIDER_SIGNALS
    )

    assert counter.clients_built == 1
    assert counter.requests_made == 1
    assert summary.ai_processed == 1
    assert summary.prefilter_accepted == 1
    assert summary.stale_skipped == 0

    written = fields_for(updates, "recGoHighLevel")
    assert written[rp.FIELD_QUALIFIED] is True


def test_the_fresh_provider_request_passes_every_pre_ai_check():
    result = prefilter_post(
        GOHIGHLEVEL_PROVIDER,
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
        max_post_age_days=MAX_AGE,
        comment_count=3,
    )

    assert result.passed is True
    assert result.is_stale is False
    assert result.service_matched is True
    assert result.intent_type in intent.AI_ELIGIBLE_INTENTS
    assert result.rejection_codes == []
    assert result.blocks_ai_call is False


def test_the_same_request_one_day_past_the_limit_does_not(monkeypatch):
    """MAX_POST_AGE_DAYS=5 means five days. Six is stale."""
    record = make_record(
        "recSixDays",
        text=GOHIGHLEVEL_PROVIDER,
        days_old=6,
        prequalification="Send to AI",
    )

    summary, _, counter = run_queue(
        monkeypatch, [record], response_json=PROVIDER_SIGNALS
    )

    assert counter.clients_built == 0
    assert summary.stale_skipped == 1


# ---------------------------------------------------------------------------
# Mixed batch -- the shape of a real run
# ---------------------------------------------------------------------------

def test_a_mixed_batch_spends_calls_only_on_the_live_lead(monkeypatch):
    records = [
        make_record("recStale", text=GOHIGHLEVEL_PROVIDER,
                    days_old=STALE_DAYS, prequalification="Send to AI"),
        make_record("recPromo", text=PROMOTIONAL_AGENCY, days_old=1,
                    human_decision="Approve"),
        make_record("recAirbnb", text=AIRBNB_TENANCY_ADVICE, days_old=2,
                    prequalification="Send to AI"),
        make_record("recRejected", text=GOHIGHLEVEL_PROVIDER, days_old=1,
                    human_decision="Reject"),
        make_record("recLive", text=GOHIGHLEVEL_PROVIDER, days_old=1),
    ]

    summary, updates, counter = run_queue(
        monkeypatch, records, response_json=PROVIDER_SIGNALS
    )

    assert counter.requests_made == 1
    assert summary.ai_processed == 1
    assert summary.stale_skipped == 1
    assert summary.human_rejected == 1
    assert len(updates) == len(records)
    assert fields_for(updates, "recLive")[rp.FIELD_QUALIFIED] is True
