"""
Tests for pipeline wiring.

These confirm that `run_pipeline` imports without credentials, that the AI
schema matches the documented signal list, and that prioritisation and
Airtable mapping behave as specified.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import run_pipeline as rp
from qualification import TIER_MANUAL_REVIEW, evaluate_lead

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Lazy initialisation
# ---------------------------------------------------------------------------

def test_module_imports_without_credentials():
    """Importing the pipeline must not require any secrets."""
    assert rp.QUALIFICATION_THRESHOLD == 65


def test_openai_client_is_not_created_at_import():
    """The OpenAI client must be lazy so tests can run without a key."""
    rp.reset_openai_client()

    assert rp._OPENAI_CLIENT is None


def test_require_env_raises_a_clear_error(monkeypatch):
    monkeypatch.delenv("AIRTABLE_BASE_ID", raising=False)

    with pytest.raises(RuntimeError, match="AIRTABLE_BASE_ID"):
        rp.airtable_url()


def test_env_int_falls_back_on_invalid_value(monkeypatch):
    monkeypatch.setenv("SOME_INT", "not-an-int")

    assert rp.env_int("SOME_INT", 42) == 42


def test_env_bool_parsing(monkeypatch):
    monkeypatch.setenv("FLAG", "true")
    assert rp.env_bool("FLAG") is True

    monkeypatch.setenv("FLAG", "0")
    assert rp.env_bool("FLAG") is False


# ---------------------------------------------------------------------------
# Thresholds and schema
# ---------------------------------------------------------------------------

def test_default_thresholds():
    assert rp.QUALIFICATION_THRESHOLD == 65
    assert rp.MANUAL_REVIEW_THRESHOLD == 55
    assert rp.HOT_LEAD_THRESHOLD == 80


REQUIRED_SIGNAL_FIELDS = [
    "intent_strength",
    "business_context",
    "buyer_role",
    "service_categories",
    "problem_specificity",
    "purchase_signal",
    "urgency",
    "business_impact",
    "location",
    "resolved_status",
    "provider_already_selected",
    "personal_request",
    "free_only_request",
    "promotional_post",
    "competitor_or_agency",
    "spam_risk",
    "outreach_appropriateness",
    "classification_confidence",
    "disqualifier_codes",
]


@pytest.mark.parametrize("field_name", REQUIRED_SIGNAL_FIELDS)
def test_schema_contains_every_structured_signal(field_name):
    assert field_name in rp.LEAD_SCHEMA["properties"]
    assert field_name in rp.LEAD_SCHEMA["required"]


def test_schema_does_not_let_the_model_decide_qualification():
    """The model must not return a score or a qualified flag."""
    assert "qualified" not in rp.LEAD_SCHEMA["properties"]
    assert "lead_score" not in rp.LEAD_SCHEMA["properties"]


def test_schema_is_strict():
    assert rp.LEAD_SCHEMA["additionalProperties"] is False
    assert set(rp.LEAD_SCHEMA["required"]) == set(
        rp.LEAD_SCHEMA["properties"]
    )


def test_instructions_tell_the_model_not_to_decide():
    assert "You do NOT decide" in rp.SYSTEM_INSTRUCTIONS
    assert "do NOT produce a score" in rp.SYSTEM_INSTRUCTIONS


def test_instructions_forbid_generic_dm():
    assert "Never write a generic template" in rp.SYSTEM_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Airtable mapping
# ---------------------------------------------------------------------------

BASE_SIGNALS = {
    "intent_strength": "strong",
    "service_categories": ["website_development"],
    "problem_specificity": "detailed",
    "business_context": "unclear",
    "purchase_signal": "researching",
    "buyer_role": "employee",
    "resolved_status": "unresolved",
    "spam_risk": "none",
    "outreach_appropriateness": "appropriate",
    "classification_confidence": 0.9,
    "disqualifier_codes": [],
    "lead_summary": "Needs a new website.",
    "evidence": "Asked for a developer.",
    "service_match": "Website development",
}


def test_manual_review_record_is_labelled_and_not_qualified():
    decision = evaluate_lead(
        {**BASE_SIGNALS, "buyer_role": "unknown"},
        suggested_dm="A specific message.",
        recommended_channel="direct_message",
        now=NOW,
    )
    assert decision.tier == TIER_MANUAL_REVIEW

    payload = rp.map_decision_to_airtable(decision, BASE_SIGNALS)

    assert payload[rp.FIELD_QUALIFIED] is False
    assert payload[rp.FIELD_SUGGESTED_DM] == ""
    assert payload[rp.FIELD_MANUAL_REVIEW] is True
    assert payload[rp.FIELD_OUTREACH_READY] is False
    assert payload[rp.FIELD_LEAD_TIER] == TIER_MANUAL_REVIEW
    assert payload[rp.FIELD_LEAD_SUMMARY].startswith("[MANUAL REVIEW]")


def test_qualified_record_maps_cleanly():
    decision = evaluate_lead(
        BASE_SIGNALS,
        suggested_dm="A specific message about their website.",
        recommended_channel="direct_message",
        now=NOW,
    )

    payload = rp.map_decision_to_airtable(
        decision, BASE_SIGNALS, prefilter_score=70
    )

    assert payload[rp.FIELD_QUALIFIED] is True
    assert payload[rp.FIELD_LEAD_SCORE] == 65
    assert payload[rp.FIELD_LEAD_TIER] == "Qualified"
    assert payload[rp.FIELD_OUTREACH_READY] is True
    assert payload[rp.FIELD_PREFILTER_SCORE] == 70
    assert payload[rp.FIELD_AI_STATUS] == "Processed"


def test_hard_rejected_record_records_its_codes():
    decision = evaluate_lead(
        {**BASE_SIGNALS, "promotional_post": True},
        suggested_dm="A specific message.",
        recommended_channel="direct_message",
        now=NOW,
    )

    payload = rp.map_decision_to_airtable(decision, BASE_SIGNALS)

    assert payload[rp.FIELD_LEAD_TIER] == "Rejected"
    assert "PROMOTIONAL_POST" in payload[rp.FIELD_DISQUALIFIERS]
    assert payload[rp.FIELD_SUGGESTED_DM] == ""
    assert payload[rp.FIELD_RECOMMENDED_CHANNEL] == "do_not_contact"


def test_extended_fields_can_be_stripped_for_older_bases():
    stripped = rp._strip_extended_fields(
        [{"id": "rec1", "fields": {rp.FIELD_QUALIFIED: True,
                                   rp.FIELD_LEAD_TIER: "Hot"}}]
    )

    assert rp.FIELD_LEAD_TIER not in stripped[0]["fields"]
    assert rp.FIELD_QUALIFIED in stripped[0]["fields"]


# ---------------------------------------------------------------------------
# Prioritisation
# ---------------------------------------------------------------------------

def record(record_id, *, text, days_old, ):
    posted = NOW - timedelta(days=days_old)
    return {
        "id": record_id,
        "fields": {
            rp.FIELD_TEXT: text,
            rp.FIELD_TIME: posted.isoformat(),
            rp.FIELD_URL: f"https://www.facebook.com/groups/1/posts/{record_id}",
        },
    }


STRONG_TEXT = (
    "Our Toronto clinic needs a new website with an online booking system "
    "and payment integration. Can anyone recommend a developer? Our current "
    "WordPress site is broken and we need help urgently."
)

WEAK_TEXT = (
    "Just sharing some thoughts about running a small business this year "
    "and how things have been going for everyone lately in the group."
)


def test_records_imported_this_run_come_first():
    older_new = record("recNew", text=STRONG_TEXT, days_old=10)
    newer_old = record("recOld", text=STRONG_TEXT, days_old=1)

    ordered = rp.prioritize_ai_queue(
        [newer_old, older_new], {"recNew"}, now=NOW
    )

    assert [r["id"] for r, _ in ordered] == ["recNew", "recOld"]


def test_newer_posts_come_before_older_ones():
    ordered = rp.prioritize_ai_queue(
        [
            record("recA", text=STRONG_TEXT, days_old=20),
            record("recB", text=STRONG_TEXT, days_old=2),
        ],
        set(),
        now=NOW,
    )

    assert [r["id"] for r, _ in ordered] == ["recB", "recA"]


def test_stronger_prefilter_score_wins_at_equal_recency():
    ordered = rp.prioritize_ai_queue(
        [
            record("recWeak", text=WEAK_TEXT, days_old=3),
            record("recStrong", text=STRONG_TEXT, days_old=3),
        ],
        set(),
        now=NOW,
    )

    assert [r["id"] for r, _ in ordered] == ["recStrong", "recWeak"]


def test_prioritization_attaches_prefilter_results():
    ordered = rp.prioritize_ai_queue(
        [record("recA", text=STRONG_TEXT, days_old=1)], set(), now=NOW
    )

    _, prefilter = ordered[0]

    assert prefilter.passed is True
    assert prefilter.score > 0


def test_prefilter_rejection_update_is_marked_rejected():
    ordered = rp.prioritize_ai_queue(
        [record("recWeak", text=WEAK_TEXT, days_old=1)], set(), now=NOW
    )
    _, prefilter = ordered[0]

    update = rp.build_prefilter_rejection_update("recWeak", prefilter)

    assert update["fields"][rp.FIELD_QUALIFIED] is False
    assert update["fields"][rp.FIELD_LEAD_TIER] == "Rejected"
    assert update["fields"][rp.FIELD_SUGGESTED_DM] == ""


# ---------------------------------------------------------------------------
# Queue formula
# ---------------------------------------------------------------------------

def test_queue_formula_does_not_require_airtable_prequalification_by_default():
    formula = rp.build_ai_queue_formula()

    assert "Prequalification" not in formula
    assert "AI Status" in formula
