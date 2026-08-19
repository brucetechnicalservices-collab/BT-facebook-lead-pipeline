"""
Regression tests for defects found in the pre-merge production readiness review.

Four defects, all confirmed against running code before being fixed:

1. A human override bypassed the prefilter but was then vetoed by the same
   intent heuristic at decision time — after the OpenAI call had been paid
   for. (The *source* of that override was itself wrong, and was corrected
   later: see tests/test_pre_ai_gating.py. These tests exercise the rule, and
   pass ``human_override`` directly.)
2. ``prefilter_post``'s early returns (too short, stale) reported the
   dataclass default ``UNRELATED`` without classifying, writing factually
   wrong intent diagnostics into Airtable.
3. The schema preflight validated 12 of the 29 fields the pipeline writes, so
   a missing ``AI Output`` failed mid-batch instead of failing clearly.
4. The Raw Signals read-only guard was applied to Facebook Group Performance,
   where ``Contacted`` is a computed count rather than a human checkbox, so
   that metric was silently dropped on every run.

Also covers the review's confirmations: business pain stays eligible, tool
research never auto-contacts, and the retry store migrates cleanly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import ai_retry
import group_performance as gp
import intent
import run_pipeline as rp
from qualification import (
    REJECT_ALREADY_RESOLVED,
    REJECT_NO_BUYING_INTENT,
    TIER_REJECTED,
    evaluate_lead,
    prefilter_post,
)

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)

#: The exact post named in the production readiness review. It must stay
#: eligible for qualification without the author saying "looking to hire".
ELECTRICAL_BUSINESS_PAIN = (
    "I own an electrical company. We're still using pen and paper to schedule "
    "jobs and I'm answering customer calls and texts all evening. I need a "
    "better way to manage this."
)

STRONG_SIGNALS = {
    "intent_strength": "moderate",
    "service_categories": ["workflow_automation", "crm"],
    "problem_specificity": "detailed",
    "business_context": "small_business",
    "purchase_signal": "researching",
    "urgency": "medium",
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
    "classification_confidence": 0.85,
    "disqualifier_codes": [],
}

SPECIFIC_DM = (
    "Hi, saw your post about scheduling jobs on paper. We set up job "
    "scheduling for trades at brucetech.ca. Worth a chat?"
)


# ---------------------------------------------------------------------------
# Review item 1 — NO_BUYING_INTENT must not reject legitimate business pain
# ---------------------------------------------------------------------------

def test_named_business_pain_post_is_classified_as_business_pain():
    result = intent.classify_intent(ELECTRICAL_BUSINESS_PAIN)

    assert result.intent_type == intent.INTENT_BUSINESS_PAIN
    assert result.is_ai_eligible is True


def test_named_business_pain_post_reaches_the_ai():
    result = prefilter_post(
        ELECTRICAL_BUSINESS_PAIN,
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
        comment_count=5,
    )

    assert result.passed is True
    assert result.service_matched is True


def test_named_business_pain_post_qualifies_without_asking_to_hire():
    """
    The author never says "looking to hire". Credible business context, direct
    service fit, high specificity, and supporting AI evidence are enough.
    """
    decision = evaluate_lead(
        STRONG_SIGNALS,
        suggested_dm=SPECIFIC_DM,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_BUSINESS_PAIN,
        post_text=ELECTRICAL_BUSINESS_PAIN,
    )

    assert REJECT_NO_BUYING_INTENT not in decision.hard_rejection_codes
    assert decision.hard_rejection_codes == []
    assert decision.qualified is True
    assert decision.tier != TIER_REJECTED


def test_no_buying_intent_can_never_fire_on_an_outreach_intent():
    """Structural guarantee, not a spot check."""
    for intent_type in sorted(intent.OUTREACH_INTENTS):
        decision = evaluate_lead(
            STRONG_SIGNALS,
            suggested_dm=SPECIFIC_DM,
            recommended_channel="direct_message",
            now=NOW,
            intent_type=intent_type,
        )

        assert REJECT_NO_BUYING_INTENT not in decision.hard_rejection_codes


def test_no_buying_intent_still_fires_on_general_advice():
    """The veto must keep working where it is supposed to."""
    decision = evaluate_lead(
        STRONG_SIGNALS,
        suggested_dm=SPECIFIC_DM,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_UNRELATED,
    )

    assert REJECT_NO_BUYING_INTENT in decision.hard_rejection_codes


# ---------------------------------------------------------------------------
# Defect 1 — a human's Approve must survive the decision
# ---------------------------------------------------------------------------

def test_human_approve_override_is_not_vetoed_by_intent():
    """
    DEFECT: a reviewed record bypassed the prefilter, cost an OpenAI call, and
    was then hard-rejected by the very heuristic the human overrode.
    """
    post = "Our office network keeps dropping and nobody can print."
    classified = intent.classify_intent(post)

    # Precondition: the classifier does not recognise this phrasing.
    assert classified.intent_type not in intent.OUTREACH_INTENTS

    decision = evaluate_lead(
        STRONG_SIGNALS,
        suggested_dm=SPECIFIC_DM,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=classified.intent_type,
        post_text=post,
        human_override=True,
    )

    assert REJECT_NO_BUYING_INTENT not in decision.hard_rejection_codes
    assert decision.qualified is True


def test_without_the_override_the_same_record_is_still_vetoed():
    post = "Our office network keeps dropping and nobody can print."

    decision = evaluate_lead(
        STRONG_SIGNALS,
        suggested_dm=SPECIFIC_DM,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.classify_intent(post).intent_type,
        post_text=post,
        human_override=False,
    )

    assert REJECT_NO_BUYING_INTENT in decision.hard_rejection_codes


def test_human_override_does_not_lift_other_hard_rejections():
    """It suppresses the intent veto only. Everything else still applies."""
    decision = evaluate_lead(
        {**STRONG_SIGNALS, "resolved_status": "resolved"},
        suggested_dm=SPECIFIC_DM,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_UNRELATED,
        human_override=True,
    )

    assert REJECT_ALREADY_RESOLVED in decision.hard_rejection_codes
    assert decision.qualified is False


def test_human_override_does_not_lift_outreach_gating():
    """
    A human is already in the loop and will see the record. The override
    unblocks qualification, never an automatic DM on an intent we do not
    consider contactable.
    """
    decision = evaluate_lead(
        STRONG_SIGNALS,
        suggested_dm=SPECIFIC_DM,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_UNRELATED,
        human_override=True,
    )

    assert decision.qualified is True
    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""


def test_qualify_post_threads_the_override_through(monkeypatch):
    """The wiring, not just the rule."""
    captured: dict[str, object] = {}

    def fake_extract(fields):
        return {**STRONG_SIGNALS, "suggested_dm": SPECIFIC_DM,
                "recommended_channel": "direct_message",
                "lead_summary": "x", "evidence": "y", "service_match": "CRM"}

    # Bind the real function before patching the name it is looked up under.
    real_evaluate = rp.evaluate_lead

    def fake_evaluate(signals, **kwargs):
        captured.update(kwargs)
        return real_evaluate(signals, **kwargs)

    monkeypatch.setattr(rp, "extract_signals", fake_extract)
    monkeypatch.setattr(rp, "evaluate_lead", fake_evaluate)

    rp.qualify_post(
        {rp.FIELD_TEXT: "anything"},
        prefilter=prefilter_post(ELECTRICAL_BUSINESS_PAIN),
        human_override=True,
    )

    assert captured["human_override"] is True


# ---------------------------------------------------------------------------
# Defect 2 — prefilter early returns must report the real intent
# ---------------------------------------------------------------------------

def test_short_post_still_reports_its_real_intent():
    """
    DEFECT: a too-short post was recorded as UNRELATED without ever being
    classified, writing wrong diagnostics into Airtable.
    """
    post = "What CRM are other contractors using?"

    assert len(post) < 40  # triggers the early return
    result = prefilter_post(post)

    assert result.passed is False
    assert result.intent_type == intent.INTENT_TOOL_RESEARCH
    assert result.service_matched is True
    assert "too short" in " ".join(result.reasons)


def test_stale_post_still_reports_its_real_intent():
    post = (
        "Looking for a web developer to rebuild our WordPress site with "
        "online booking and payments. Toronto area preferred."
    )
    result = prefilter_post(
        post,
        post_time=(NOW - timedelta(days=90)).isoformat(),
        now=NOW,
    )

    assert result.passed is False
    assert result.intent_type == intent.INTENT_PROVIDER_REQUEST
    assert result.service_matched is True
    assert "older than" in " ".join(result.reasons)


def test_rejection_diagnostics_written_to_airtable_are_accurate():
    """The wrong values were being persisted, which is why this matters."""
    result = prefilter_post("What CRM are other contractors using?")
    update = rp.build_prefilter_rejection_update("recShort", result)

    diagnostics = json.loads(update["fields"][rp.FIELD_AI_OUTPUT])

    assert diagnostics["intent_type"] == intent.INTENT_TOOL_RESEARCH
    assert diagnostics["service_matched"] is True


def test_empty_post_is_still_unrelated():
    result = prefilter_post("")

    assert result.passed is False
    assert result.intent_type == intent.INTENT_UNRELATED


# ---------------------------------------------------------------------------
# Review item 2 — tool research never auto-contacts
# ---------------------------------------------------------------------------

def test_tool_research_never_becomes_outreach_ready_even_at_a_perfect_score():
    decision = evaluate_lead(
        {**STRONG_SIGNALS, "intent_strength": "strong",
         "purchase_signal": "ready_to_buy", "urgency": "high",
         "business_context": "established_business",
         "classification_confidence": 1.0},
        suggested_dm="A genuinely specific personalised message.",
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_TOOL_RESEARCH,
    )

    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""
    assert decision.recommended_channel == "do_not_contact"


def test_tool_research_is_not_an_outreach_intent():
    assert intent.INTENT_TOOL_RESEARCH not in intent.OUTREACH_INTENTS


# ---------------------------------------------------------------------------
# Defect 3 — the schema preflight must cover every writable field
# ---------------------------------------------------------------------------

def _all_written_fields() -> set[str]:
    from qualification import LeadDecision

    decision = LeadDecision(
        lead_score=70, tier="Qualified", qualified=True, hard_rejected=False
    )
    prefilter = prefilter_post(ELECTRICAL_BUSINESS_PAIN)

    written = set()
    written |= set(
        rp.map_decision_to_airtable(
            decision, {"lead_summary": "x"}, prefilter_score=50,
            prefilter=prefilter,
        )
    )
    written |= set(
        rp.build_prefilter_rejection_update("r", prefilter)["fields"]
    )
    written |= set(
        rp.build_error_payload("e", attempts=1, transient=True)
    )
    written |= set(
        rp.map_apify_to_airtable(
            {"url": "u", "user": {}, "time": "t"},
            apify_run_id="R", scraper_run_record_id="rec1",
        )
    )
    return written


def test_every_written_field_is_schema_validated():
    """
    DEFECT: only 12 of 29 written fields were validated, so a missing
    AI Output failed mid-batch rather than at the preflight.
    """
    unvalidated = _all_written_fields() - set(rp.REQUIRED_RAW_SIGNAL_FIELDS)

    assert unvalidated == set(), (
        f"written but not validated by the preflight: {sorted(unvalidated)}"
    )


def test_ai_output_is_validated_because_retry_state_depends_on_it():
    assert rp.FIELD_AI_OUTPUT in rp.REQUIRED_RAW_SIGNAL_FIELDS


def test_no_formula_or_human_field_is_ever_validated_as_writable():
    assert not set(rp.REQUIRED_RAW_SIGNAL_FIELDS) & rp.READ_ONLY_FIELDS


def test_no_formula_or_human_field_is_ever_written():
    assert not _all_written_fields() & rp.READ_ONLY_FIELDS


# ---------------------------------------------------------------------------
# Defect 4 — the read-only guard must be scoped per table
# ---------------------------------------------------------------------------

def test_group_performance_contacted_count_is_not_stripped():
    """
    DEFECT: "Contacted" is a read-only checkbox on Raw Signals and a computed
    count on Group Performance. Applying the Raw Signals guard to the group
    table silently dropped the metric on every run.
    """
    metrics = gp.GroupMetrics(group_name="Toronto Trades", contacted=5)
    payload = metrics.to_fields()

    survived = rp.strip_read_only_fields(payload, gp.NEVER_WRITE_FIELDS)

    assert survived[gp.FIELD_CONTACTED] == 5
    assert set(survived) == set(payload)


def test_group_guard_still_blocks_human_owned_columns():
    payload = {
        gp.FIELD_POSTS_SCRAPED: 10,
        gp.FIELD_TIER: "A",
        gp.FIELD_STATUS: "Active",
        gp.FIELD_REVENUE: 9000,
        gp.FIELD_NOTES: "hand written",
    }

    survived = rp.strip_read_only_fields(payload, gp.NEVER_WRITE_FIELDS)

    assert set(survived) == {gp.FIELD_POSTS_SCRAPED}


def test_raw_signals_guard_is_unchanged_by_default():
    payload = {
        rp.FIELD_LEAD_SCORE: 80,
        rp.FIELD_HUMAN_DECISION: "Approve",
        rp.FIELD_OUTREACH_STATUS: "Won",
        rp.FIELD_CONTACTED_LEGACY: True,
        rp.FIELD_LAST_CONTACTED: "2026-08-01",
    }

    survived = rp.strip_read_only_fields(payload)

    assert set(survived) == {rp.FIELD_LEAD_SCORE}


def test_scraper_run_fields_survive_their_own_guard():
    import scraper_runs

    payload = {
        scraper_runs.FIELD_APIFY_RUN_ID: "RUN1",
        scraper_runs.FIELD_RESULTS: 12,
        scraper_runs.FIELD_COST: 0.4,
    }

    survived = rp.strip_read_only_fields(
        payload, rp.SCRAPER_RUNS_PROTECTED_FIELDS
    )

    assert set(survived) == set(payload)


# ---------------------------------------------------------------------------
# Review item 7 — retry storage is isolated behind a migration seam
# ---------------------------------------------------------------------------

def test_retry_store_defaults_to_the_ai_output_blob():
    store = ai_retry.RetryStore(output_field="AI Output")

    assert store.uses_dedicated_field is False

    payload = store.build_failure_fields(
        "boom", attempts=2, transient=True,
        status_field="AI Status", version="facebook-v2",
    )

    assert json.loads(payload["AI Output"])["attempts"] == 2
    assert store.read_attempts(payload) == 2


def test_retry_store_migrates_to_a_dedicated_field_without_other_changes():
    """
    The point of the seam: adding an AI Attempts field is configuration, not
    a code change, and nothing in the qualification architecture moves.
    """
    store = ai_retry.RetryStore(
        output_field="AI Output", attempts_field="AI Attempts"
    )

    assert store.uses_dedicated_field is True

    payload = store.build_failure_fields(
        "boom", attempts=2, transient=True,
        status_field="AI Status", version="facebook-v2",
    )

    assert payload["AI Attempts"] == 2
    assert store.read_attempts(payload) == 2
    # Diagnostics stay readable; the count no longer rides in the blob.
    assert "attempts" not in json.loads(payload["AI Output"])


def test_dedicated_field_wins_over_a_stale_blob():
    store = ai_retry.RetryStore(
        output_field="AI Output", attempts_field="AI Attempts"
    )

    fields = {"AI Attempts": 3, "AI Output": json.dumps({"attempts": 1})}

    assert store.read_attempts(fields) == 3


def test_dedicated_field_falls_back_to_the_blob_when_blank():
    store = ai_retry.RetryStore(
        output_field="AI Output", attempts_field="AI Attempts"
    )

    fields = {"AI Attempts": None, "AI Output": json.dumps({"attempts": 2})}

    assert store.read_attempts(fields) == 2


@pytest.mark.parametrize(
    "raw", ["", None, "legacy plain text", "[]", "{bad json", "42"]
)
def test_unparseable_output_reads_as_zero_attempts(raw):
    store = ai_retry.RetryStore(output_field="AI Output")

    assert store.read_attempts({"AI Output": raw}) == 0


def test_permanent_failure_consumes_the_whole_budget_at_once():
    store = ai_retry.RetryStore(output_field="AI Output")

    assert store.next_attempt_count(
        {}, transient=False, max_attempts=3
    ) == 3


def test_transient_failure_increments_by_one():
    store = ai_retry.RetryStore(output_field="AI Output")
    fields = {"AI Output": json.dumps({"attempts": 1})}

    assert store.next_attempt_count(
        fields, transient=True, max_attempts=3
    ) == 2


def test_failure_payload_never_touches_lead_fields():
    payload = rp.build_error_payload("boom", attempts=1, transient=True)

    assert set(payload) == {rp.FIELD_AI_STATUS, rp.FIELD_AI_OUTPUT}


def test_run_pipeline_delegates_to_the_store():
    assert isinstance(rp.RETRY_STORE, ai_retry.RetryStore)
    assert rp.RETRY_STORE.output_field == rp.FIELD_AI_OUTPUT
    assert rp.is_transient_error is ai_retry.is_transient_error
