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


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_dry_run_is_off_by_default():
    assert rp.DRY_RUN is False


def test_dry_run_makes_no_airtable_writes(monkeypatch, capsys):
    """DRY_RUN must never issue an HTTP request to Airtable."""
    monkeypatch.setattr(rp, "DRY_RUN", True)

    def explode(*args, **kwargs):
        raise AssertionError("DRY_RUN must not make HTTP requests")

    monkeypatch.setattr(rp, "request_with_retry", explode)

    rp.update_airtable_records(
        [{"id": "rec1", "fields": {rp.FIELD_LEAD_TIER: "Hot",
                                   rp.FIELD_LEAD_SCORE: 90,
                                   rp.FIELD_QUALIFIED: True}}]
    )
    rp.mark_ai_error("rec2", "boom")
    created = rp.create_new_posts_in_airtable(
        [{"url": "https://www.facebook.com/groups/1/posts/1"}]
    )

    assert created == []
    output = capsys.readouterr().out
    assert "[DRY RUN]" in output
    assert "rec1" in output


def test_dry_run_reports_the_intended_decision(monkeypatch, capsys):
    monkeypatch.setattr(rp, "DRY_RUN", True)

    rp.update_airtable_records(
        [{"id": "rec1", "fields": {rp.FIELD_LEAD_TIER: "Manual Review",
                                   rp.FIELD_LEAD_SCORE: 60,
                                   rp.FIELD_QUALIFIED: False,
                                   rp.FIELD_OUTREACH_READY: False,
                                   rp.FIELD_SUGGESTED_DM: ""}}]
    )

    output = capsys.readouterr().out
    assert "tier=Manual Review" in output
    assert "score=60" in output
    assert "dm=no" in output


# ---------------------------------------------------------------------------
# Human "Send to AI" selection
# ---------------------------------------------------------------------------

def flagged_record(record_id, *, text, days_old, flagged):
    entry = record(record_id, text=text, days_old=days_old)
    if flagged:
        entry["fields"][rp.FIELD_PREQUALIFICATION] = "Send to AI"
    return entry


def test_is_human_flagged_matches_case_insensitively():
    assert rp.is_human_flagged({rp.FIELD_PREQUALIFICATION: "Send to AI"})
    assert rp.is_human_flagged({rp.FIELD_PREQUALIFICATION: "send to ai "})
    assert not rp.is_human_flagged({rp.FIELD_PREQUALIFICATION: "Skip"})
    assert not rp.is_human_flagged({})


def test_human_flagged_records_are_processed_first():
    """A curated selection outranks recency and prefilter score."""
    ordered = rp.prioritize_ai_queue(
        [
            flagged_record("recNewStrong", text=STRONG_TEXT, days_old=1,
                           flagged=False),
            flagged_record("recOldWeak", text=WEAK_TEXT, days_old=90,
                           flagged=True),
        ],
        set(),
        now=NOW,
    )

    assert [r["id"] for r, _ in ordered] == ["recOldWeak", "recNewStrong"]


def test_human_flagged_record_outranks_records_imported_this_run():
    ordered = rp.prioritize_ai_queue(
        [
            flagged_record("recImported", text=STRONG_TEXT, days_old=1,
                           flagged=False),
            flagged_record("recFlagged", text=WEAK_TEXT, days_old=30,
                           flagged=True),
        ],
        {"recImported"},
        now=NOW,
    )

    assert [r["id"] for r, _ in ordered] == ["recFlagged", "recImported"]


def test_prequalified_formula_requires_send_to_ai_and_unprocessed():
    formula = rp.build_prequalified_queue_formula()

    assert "Send to AI" in formula
    assert "AI Status" in formula


# ---------------------------------------------------------------------------
# Airtable query construction
# ---------------------------------------------------------------------------

def test_list_airtable_records_sends_sort_params(monkeypatch):
    """
    A truncated fetch must be sorted server-side.

    Without a sort, Airtable returns table order, so max_records yields an
    arbitrary slice rather than the newest records.
    """
    captured = {}

    class FakeResponse:
        @staticmethod
        def json():
            return {"records": []}

    def fake_request(method, url, *, headers=None, params=None, **kwargs):
        captured["params"] = params
        return FakeResponse()

    monkeypatch.setenv("AIRTABLE_BASE_ID", "appX")
    monkeypatch.setenv("AIRTABLE_TABLE_NAME", "Leads")
    monkeypatch.setenv("AIRTABLE_TOKEN", "tok")
    monkeypatch.setattr(rp, "request_with_retry", fake_request)

    rp.list_airtable_records(
        formula="x", max_records=10, sort_field=rp.FIELD_TIME
    )

    params = dict(captured["params"])
    assert params["sort[0][field]"] == rp.FIELD_TIME
    assert params["sort[0][direction]"] == "desc"


def test_list_airtable_records_omits_sort_when_not_requested(monkeypatch):
    captured = {}

    class FakeResponse:
        @staticmethod
        def json():
            return {"records": []}

    def fake_request(method, url, *, headers=None, params=None, **kwargs):
        captured["params"] = params
        return FakeResponse()

    monkeypatch.setenv("AIRTABLE_BASE_ID", "appX")
    monkeypatch.setenv("AIRTABLE_TABLE_NAME", "Leads")
    monkeypatch.setenv("AIRTABLE_TOKEN", "tok")
    monkeypatch.setattr(rp, "request_with_retry", fake_request)

    rp.list_airtable_records(formula="x")

    assert "sort[0][field]" not in dict(captured["params"])
