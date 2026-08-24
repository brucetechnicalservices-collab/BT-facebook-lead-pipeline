"""
Pipeline-level regression tests for the intent and attribution release.

Covers the wiring that the pure-module tests cannot: Raw Signal linkage to a
scraper run, preservation of human decisions and sales outcomes, bounded AI
error retries, and the read-only field guard.

Every external call is mocked. Nothing here starts an Apify run, writes to
Airtable, or contacts OpenAI, and the whole file runs without OPENAI_API_KEY.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import intent
import run_pipeline as rp
from tests.fixtures import (
    CRM_RESEARCH,
    EXCAVATION_FUNDING,
    GOHIGHLEVEL_PROVIDER,
    PEN_AND_PAPER_ELECTRICIAN,
    VIRTUAL_ASSISTANT,
)

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)

APIFY_POST = {
    "legacyId": "999",
    "url": "https://www.facebook.com/groups/1/posts/999",
    "text": GOHIGHLEVEL_PROVIDER,
    "user": {"id": "u9", "name": "Robin"},
    "groupTitle": "Toronto Trades",
    "time": "2026-08-17T09:00:00Z",
    "commentsCount": 4,
}


def record(record_id, *, text, days_old=1, **extra):
    posted = NOW - timedelta(days=days_old)
    fields = {
        rp.FIELD_TEXT: text,
        rp.FIELD_TIME: posted.isoformat(),
        rp.FIELD_URL: f"https://www.facebook.com/groups/1/posts/{record_id}",
        **extra,
    }
    return {"id": record_id, "fields": fields}


# ---------------------------------------------------------------------------
# Requirement 25: every imported Raw Signal carries its run attribution
# ---------------------------------------------------------------------------

def test_imported_post_receives_run_id_and_scraper_run_link():
    mapped = rp.map_apify_to_airtable(
        APIFY_POST,
        apify_run_id="RUN123",
        scraper_run_record_id="recScraperRun1",
    )

    assert mapped[rp.FIELD_APIFY_RUN_ID] == "RUN123"
    # Linked-record fields take a list of Airtable record IDs.
    assert mapped[rp.FIELD_SCRAPER_RUN] == ["recScraperRun1"]


def test_import_without_a_logged_run_still_records_the_run_id():
    """If the run log write failed, posts must still be attributable."""
    mapped = rp.map_apify_to_airtable(APIFY_POST, apify_run_id="RUN123")

    assert mapped[rp.FIELD_APIFY_RUN_ID] == "RUN123"
    assert rp.FIELD_SCRAPER_RUN not in mapped


def test_import_without_attribution_omits_both_fields():
    mapped = rp.map_apify_to_airtable(APIFY_POST)

    assert rp.FIELD_APIFY_RUN_ID not in mapped
    assert rp.FIELD_SCRAPER_RUN not in mapped


def test_core_post_fields_are_still_mapped():
    mapped = rp.map_apify_to_airtable(APIFY_POST, apify_run_id="RUN123")

    assert mapped[rp.FIELD_USER_NAME] == "Robin"
    assert mapped[rp.FIELD_GROUP_TITLE] == "Toronto Trades"
    assert mapped[rp.FIELD_COMMENTS] == 4
    assert mapped[rp.FIELD_TIME] == "2026-08-17T09:00:00Z"


# ---------------------------------------------------------------------------
# Requirements 29 and 30: human decisions and sales outcomes are preserved
# ---------------------------------------------------------------------------

def test_human_decision_is_never_written():
    payload = {
        rp.FIELD_QUALIFIED: True,
        rp.FIELD_HUMAN_DECISION: "Approve",
    }

    assert rp.FIELD_HUMAN_DECISION not in rp.strip_read_only_fields(payload)
    assert rp.FIELD_QUALIFIED in rp.strip_read_only_fields(payload)


def test_outreach_status_is_never_written():
    payload = {rp.FIELD_OUTREACH_STATUS: "Won", rp.FIELD_LEAD_SCORE: 80}

    stripped = rp.strip_read_only_fields(payload)

    assert rp.FIELD_OUTREACH_STATUS not in stripped
    assert stripped[rp.FIELD_LEAD_SCORE] == 80


@pytest.mark.parametrize(
    "field_name",
    [
        rp.FIELD_PREQUALIFICATION,
        rp.FIELD_REQUEST_SIGNAL,
        rp.FIELD_SERVICE_SIGNAL,
        rp.FIELD_PROMOTION_SIGNAL,
        rp.FIELD_PROVIDER_INTENT_SIGNAL,
        rp.FIELD_RESEARCH_INTENT_SIGNAL,
        rp.FIELD_INTENT_TYPE,
        rp.FIELD_LAST_CONTACTED,
        rp.FIELD_CONTACTED_LEGACY,
    ],
)
def test_formula_and_human_fields_are_stripped(field_name):
    assert field_name not in rp.strip_read_only_fields({field_name: "x"})


def test_decision_payload_contains_no_read_only_fields():
    """The real payload, not a synthetic one."""
    from qualification import evaluate_lead

    signals = {
        "intent_strength": "strong",
        "service_categories": ["crm"],
        "problem_specificity": "detailed",
        "business_context": "small_business",
        "purchase_signal": "ready_to_buy",
        "urgency": "medium",
        "business_impact": "medium",
        "location": "toronto_gta",
        "buyer_role": "owner_or_decision_maker",
        "resolved_status": "unresolved",
        "spam_risk": "none",
        "outreach_appropriateness": "appropriate",
        "classification_confidence": 0.9,
        "disqualifier_codes": [],
        "lead_summary": "Needs a CRM implementer.",
        "evidence": "Asked for GoHighLevel setup.",
        "service_match": "CRM",
    }
    decision = evaluate_lead(
        signals,
        suggested_dm="A specific message.",
        recommended_channel="direct_message",
    )

    payload = rp.map_decision_to_airtable(decision, signals)

    assert not set(payload) & rp.READ_ONLY_FIELDS


def test_human_rejection_disqualifies_the_lead():
    codes = rp.collect_policy_disqualifiers(
        {rp.FIELD_HUMAN_DECISION: "Reject"}
    )

    assert rp.REJECT_HUMAN_REJECTED in codes


def test_human_approval_is_not_a_disqualifier():
    assert rp.collect_policy_disqualifiers(
        {rp.FIELD_HUMAN_DECISION: "Approve"}
    ) == []


@pytest.mark.parametrize(
    "status,skipped",
    [
        ("", False),
        ("Not Contacted", False),
        ("Contacted", True),
        ("Replied", True),
        ("Meeting Booked", True),
        ("Proposal Sent", True),
        ("Won", True),
        ("Lost", True),
        ("No Response", True),
        ("Do Not Contact", True),
    ],
)
def test_records_in_the_sales_funnel_are_skipped(status, skipped):
    assert rp.has_active_outreach({rp.FIELD_OUTREACH_STATUS: status}) is skipped


def test_suppressed_author_is_disqualified(monkeypatch):
    monkeypatch.setattr(rp, "SUPPRESSED_AUTHORS", frozenset({"robin"}))

    codes = rp.collect_policy_disqualifiers({rp.FIELD_USER_NAME: "Robin"})

    assert rp.REJECT_SUPPRESSED_AUTHOR in codes


def test_disallowed_group_is_disqualified(monkeypatch):
    monkeypatch.setattr(rp, "DISALLOWED_GROUPS", frozenset({"test group"}))

    codes = rp.collect_policy_disqualifiers(
        {rp.FIELD_GROUP_TITLE: "Test Group"}
    )

    assert rp.REJECT_DISALLOWED_GROUP in codes


# ---------------------------------------------------------------------------
# Requirement 31: AI failures retry, but only so many times
# ---------------------------------------------------------------------------

def test_attempts_are_read_from_the_ai_output_blob():
    fields = {
        rp.FIELD_AI_STATUS: "Error",
        rp.FIELD_AI_OUTPUT: json.dumps({"attempts": 2}),
    }

    assert rp.ai_attempts_so_far(fields) == 2


@pytest.mark.parametrize(
    "value", ["", None, "legacy plain text output", "[]", "{bad json"]
)
def test_unparseable_ai_output_reads_as_zero_attempts(value):
    """Legacy or model output must not be mistaken for an attempt counter."""
    assert rp.ai_attempts_so_far({rp.FIELD_AI_OUTPUT: value}) == 0


def test_retry_is_exhausted_at_the_configured_limit(monkeypatch):
    monkeypatch.setattr(rp, "AI_ERROR_MAX_ATTEMPTS", 3)

    exhausted = {
        rp.FIELD_AI_STATUS: "Error",
        rp.FIELD_AI_OUTPUT: json.dumps({"attempts": 3}),
    }
    retryable = {
        rp.FIELD_AI_STATUS: "Error",
        rp.FIELD_AI_OUTPUT: json.dumps({"attempts": 1}),
    }

    assert rp.is_retry_exhausted(exhausted) is True
    assert rp.is_retry_exhausted(retryable) is False


def test_a_processed_record_is_never_treated_as_retry_exhausted():
    fields = {
        rp.FIELD_AI_STATUS: "Processed",
        rp.FIELD_AI_OUTPUT: json.dumps({"attempts": 9}),
    }

    assert rp.is_retry_exhausted(fields) is False


@pytest.mark.parametrize(
    "message",
    [
        "Request timed out",
        "rate limit exceeded",
        "server returned 503",
        "Connection reset by peer",
        "The engine is currently overloaded",
    ],
)
def test_transient_errors_are_retryable(message):
    assert rp.is_transient_error(RuntimeError(message)) is True


@pytest.mark.parametrize(
    "message",
    ["invalid record: missing text", "schema validation failed"],
)
def test_permanent_errors_are_not_retryable(message):
    assert rp.is_transient_error(ValueError(message)) is False


def test_error_payload_records_the_attempt_count_and_nothing_else():
    payload = rp.build_error_payload("boom", attempts=2, transient=True)

    assert payload[rp.FIELD_AI_STATUS] == rp.AI_STATUS_ERROR

    diagnostics = json.loads(payload[rp.FIELD_AI_OUTPUT])
    assert diagnostics["attempts"] == 2
    assert diagnostics["transient"] is True
    assert diagnostics["last_error"] == "boom"

    # Crucially: a failure must not blank the record's lead analysis.
    assert rp.FIELD_LEAD_SCORE not in payload
    assert rp.FIELD_QUALIFIED not in payload
    assert rp.FIELD_LEAD_TIER not in payload

    # Changed 2026-08-19: it does withdraw outreach eligibility, so a
    # decision the pipeline could not reproduce cannot be acted on.
    assert payload[rp.FIELD_OUTREACH_READY] is False
    assert payload[rp.FIELD_SUGGESTED_DM] == ""
    assert payload[rp.FIELD_SUGGESTED_COMMENT] == ""


def test_error_records_are_requeued_only_when_retry_is_enabled(monkeypatch):
    monkeypatch.setattr(rp, "RETRY_AI_ERRORS", True)
    assert "Error" in rp.build_unprocessed_status_clause()

    monkeypatch.setattr(rp, "RETRY_AI_ERRORS", False)
    assert "Error" not in rp.build_unprocessed_status_clause()


# ---------------------------------------------------------------------------
# Qualification Version
# ---------------------------------------------------------------------------

def test_qualification_version_is_written_on_processed_records():
    from qualification import LeadDecision

    decision = LeadDecision(
        lead_score=70, tier="Qualified", qualified=True, hard_rejected=False
    )
    payload = rp.map_decision_to_airtable(decision, {"lead_summary": "x"})

    assert payload[rp.FIELD_QUALIFICATION_VERSION] == rp.QUALIFICATION_VERSION


def test_qualification_version_is_written_on_prefilter_rejections():
    from qualification import prefilter_post

    prefilter = prefilter_post(EXCAVATION_FUNDING, now=NOW)
    update = rp.build_prefilter_rejection_update("recX", prefilter)

    assert update["fields"][rp.FIELD_QUALIFICATION_VERSION] == (
        rp.QUALIFICATION_VERSION
    )


def test_prefilter_rejection_records_the_intent_for_a_human():
    from qualification import prefilter_post

    prefilter = prefilter_post(EXCAVATION_FUNDING, now=NOW)
    update = rp.build_prefilter_rejection_update("recX", prefilter)

    diagnostics = json.loads(update["fields"][rp.FIELD_AI_OUTPUT])
    assert diagnostics["intent_type"] == intent.INTENT_GENERAL_ADVICE
    assert diagnostics["service_matched"] is False
    assert diagnostics["prefiltered"] is True


def test_ai_output_holds_the_deterministic_assessment():
    """
    Airtable's Intent Type column is a formula and cannot be written, so the
    Python classification is recorded in the AI Output audit blob.
    """
    from qualification import LeadDecision, prefilter_post

    prefilter = prefilter_post(GOHIGHLEVEL_PROVIDER, now=NOW)
    decision = LeadDecision(
        lead_score=82, tier="Hot", qualified=True, hard_rejected=False
    )

    payload = rp.map_decision_to_airtable(
        decision, {"lead_summary": "x"}, prefilter=prefilter
    )
    blob = json.loads(payload[rp.FIELD_AI_OUTPUT])

    assert blob["intent_type"] == intent.INTENT_PROVIDER_REQUEST
    assert blob["qualification_version"] == rp.QUALIFICATION_VERSION
    assert blob["prefilter_score"] == prefilter.score


# ---------------------------------------------------------------------------
# Requirement 17: queue ordering favours fresh, high-intent leads
# ---------------------------------------------------------------------------

def test_provider_requests_outrank_business_pain():
    ordered = rp.prioritize_ai_queue(
        [
            record("recPain", text=PEN_AND_PAPER_ELECTRICIAN),
            record("recProvider", text=GOHIGHLEVEL_PROVIDER),
        ],
        set(),
        now=NOW,
    )

    assert [r["id"] for r, _ in ordered] == ["recProvider", "recPain"]


def test_records_imported_this_run_outrank_a_stale_backlog():
    """
    The failure this prevents: a backlog of weak posts consuming the whole
    AI_BATCH_LIMIT before today's leads are reached.
    """
    ordered = rp.prioritize_ai_queue(
        [
            record("recOldProvider", text=GOHIGHLEVEL_PROVIDER, days_old=4),
            record("recNewPain", text=PEN_AND_PAPER_ELECTRICIAN, days_old=1),
        ],
        {"recNewPain"},
        now=NOW,
    )

    assert ordered[0][0]["id"] == "recNewPain"


def test_lower_comment_competition_breaks_a_tie():
    ordered = rp.prioritize_ai_queue(
        [
            record("recBusy", text=GOHIGHLEVEL_PROVIDER, **{rp.FIELD_COMMENTS: 40}),
            record("recQuiet", text=GOHIGHLEVEL_PROVIDER, **{rp.FIELD_COMMENTS: 2}),
        ],
        set(),
        now=NOW,
    )

    assert ordered[0][0]["id"] == "recQuiet"


def test_prioritization_attaches_the_intent_classification():
    ordered = rp.prioritize_ai_queue(
        [record("recA", text=CRM_RESEARCH)], set(), now=NOW
    )
    _, prefilter = ordered[0]

    assert prefilter.intent_type == intent.INTENT_TOOL_RESEARCH


def test_solution_hopping_posts_are_prefiltered_out_of_the_queue():
    ordered = rp.prioritize_ai_queue(
        [
            record("recVA", text=VIRTUAL_ASSISTANT),
            record("recFunding", text=EXCAVATION_FUNDING),
        ],
        set(),
        now=NOW,
    )

    assert all(not prefilter.passed for _, prefilter in ordered)


# ---------------------------------------------------------------------------
# Configuration and safety
# ---------------------------------------------------------------------------

def test_deterministic_tests_run_without_an_openai_key(monkeypatch):
    """Requirement 32."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rp.reset_openai_client()

    ordered = rp.prioritize_ai_queue(
        [record("recA", text=GOHIGHLEVEL_PROVIDER)], set(), now=NOW
    )

    assert ordered[0][1].passed is True
    assert rp._OPENAI_CLIENT is None


def test_max_post_age_defaults_to_five_days():
    from qualification import DEFAULT_MAX_POST_AGE_DAYS

    assert DEFAULT_MAX_POST_AGE_DAYS == 5


def test_qualification_threshold_is_still_65():
    assert rp.QUALIFICATION_THRESHOLD == 65


def test_env_csv_parses_a_suppression_list(monkeypatch):
    monkeypatch.setenv("SOME_LIST", " Alpha , BETA ,, gamma ")

    assert rp.env_csv("SOME_LIST") == frozenset({"alpha", "beta", "gamma"})


def test_env_float_falls_back_on_invalid_values(monkeypatch):
    monkeypatch.setenv("SOME_FLOAT", "not-a-number")

    assert rp.env_float("SOME_FLOAT", 0.55) == 0.55


def test_instructions_forbid_solution_hopping():
    assert "DO NOT SOLUTION-HOP" in rp.SYSTEM_INSTRUCTIONS
    assert "virtual assistant" in rp.SYSTEM_INSTRUCTIONS.lower()


def test_instructions_forbid_inventing_details():
    for forbidden in ("urgency", "budget", "location", "business names"):
        assert forbidden in rp.SYSTEM_INSTRUCTIONS


def test_stale_post_is_not_offered_to_the_model_as_a_disqualifier():
    """STALE_POST is computed in Python and never trusted from the model."""
    from qualification import (
        ALL_DISQUALIFIER_CODES,
        KNOWN_DISQUALIFIER_CODES,
        REJECT_STALE_POST,
    )

    assert REJECT_STALE_POST not in KNOWN_DISQUALIFIER_CODES
    assert REJECT_STALE_POST in ALL_DISQUALIFIER_CODES
