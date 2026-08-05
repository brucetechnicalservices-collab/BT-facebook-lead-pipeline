"""
Regression tests for the deterministic qualification rules.

These tests import only `qualification`, which has no environment or network
dependencies, so they run without any API credentials.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from qualification import (
    DEFAULT_MANUAL_REVIEW_THRESHOLD,
    DEFAULT_MAX_POST_AGE_DAYS,
    DEFAULT_QUALIFICATION_THRESHOLD,
    REJECT_ALREADY_RESOLVED,
    REJECT_COMPETITOR_OR_AGENCY,
    REJECT_FREE_ONLY_REQUEST,
    REJECT_INAPPROPRIATE_OUTREACH,
    REJECT_JOB_SEEKER,
    REJECT_NO_BUSINESS_CONTEXT,
    REJECT_NO_SERVICE_MATCH,
    REJECT_PERSONAL_REQUEST,
    REJECT_PROMOTIONAL_POST,
    REJECT_PROVIDER_SELECTED,
    REJECT_SPAM_OR_AFFILIATE,
    REJECT_STALE_POST,
    REJECT_STUDENT_OR_EDUCATIONAL,
    TIER_HOT,
    TIER_MANUAL_REVIEW,
    TIER_QUALIFIED,
    TIER_REJECTED,
    calculate_score,
    evaluate_lead,
    prefilter_post,
)

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)

#: Signals that score exactly 65 -- the qualification threshold.
SIGNALS_AT_65 = {
    "intent_strength": "strong",
    "service_categories": ["website_development"],
    "problem_specificity": "detailed",
    "business_context": "unclear",
    "purchase_signal": "researching",
    "urgency": "none",
    "business_impact": "none",
    "location": "unknown",
    "buyer_role": "employee",
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
}

#: The strongest possible signal set: scores 100.
SIGNALS_AT_100 = {
    **SIGNALS_AT_65,
    "service_categories": ["website_development", "seo", "managed_it"],
    "business_context": "established_business",
    "purchase_signal": "ready_to_buy",
    "urgency": "high",
    "business_impact": "high",
    "location": "toronto_gta",
    "buyer_role": "owner_or_decision_maker",
    "classification_confidence": 1.0,
}


def signals(**overrides):
    """Build a signal dict from the 65-point baseline."""
    return {**SIGNALS_AT_65, **overrides}


def decide(signal_overrides=None, **kwargs):
    """Evaluate a lead with sensible test defaults."""
    params = {
        "suggested_dm": "Hi Sam, saw your post about the booking form on "
                        "your site. We do this at brucetech.ca. Want to chat?",
        "recommended_channel": "direct_message",
        "now": NOW,
        **kwargs,
    }
    return evaluate_lead(signals(**(signal_overrides or {})), **params)


# ---------------------------------------------------------------------------
# Threshold behaviour
# ---------------------------------------------------------------------------

def test_threshold_default_is_65():
    assert DEFAULT_QUALIFICATION_THRESHOLD == 65


def test_score_64_does_not_qualify():
    """One point below the threshold must not qualify."""
    decision = decide({"buyer_role": "unknown"})

    assert decision.lead_score == 64
    assert decision.qualified is False
    assert decision.tier == TIER_MANUAL_REVIEW


def test_score_65_qualifies_when_no_veto_applies():
    """Exactly at the threshold, with no hard rejection, must qualify."""
    decision = decide()

    assert decision.lead_score == 65
    assert decision.qualified is True
    assert decision.tier == TIER_QUALIFIED
    assert decision.hard_rejected is False


def test_scores_between_55_and_64_are_manual_review():
    decision = decide({"buyer_role": "unknown"})

    assert DEFAULT_MANUAL_REVIEW_THRESHOLD <= decision.lead_score < 65
    assert decision.tier == TIER_MANUAL_REVIEW


@pytest.mark.parametrize(
    "overrides,expected_tier",
    [
        ({}, TIER_QUALIFIED),
        ({"buyer_role": "unknown"}, TIER_MANUAL_REVIEW),
        ({"intent_strength": "weak", "problem_specificity": "vague"},
         TIER_REJECTED),
    ],
)
def test_tier_boundaries(overrides, expected_tier):
    assert decide(overrides).tier == expected_tier


def test_hot_tier_at_80_or_above():
    decision = evaluate_lead(
        SIGNALS_AT_100,
        suggested_dm="Specific message about their booking system.",
        recommended_channel="direct_message",
        now=NOW,
    )

    assert decision.lead_score >= 80
    assert decision.tier == TIER_HOT
    assert decision.qualified is True


def test_only_hot_and_qualified_set_qualified_true():
    """No tier other than Hot/Qualified may ever set Qualified = true."""
    for overrides in (
        {"buyer_role": "unknown"},                    # manual review
        {"intent_strength": "none"},                  # low score
        {"promotional_post": True},                   # hard rejection
    ):
        decision = decide(overrides)
        if decision.tier in (TIER_HOT, TIER_QUALIFIED):
            assert decision.qualified is True
        else:
            assert decision.qualified is False


# ---------------------------------------------------------------------------
# Hard rejections override the score
# ---------------------------------------------------------------------------

def test_hard_rejection_overrides_high_score():
    """A perfect score is still rejected when a veto applies."""
    assert calculate_score(SIGNALS_AT_100) == 100

    decision = evaluate_lead(
        {**SIGNALS_AT_100, "promotional_post": True},
        suggested_dm="A very specific message.",
        recommended_channel="direct_message",
        now=NOW,
    )

    assert decision.lead_score == 100
    assert decision.hard_rejected is True
    assert decision.tier == TIER_REJECTED
    assert decision.qualified is False
    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""


def test_resolved_request_is_rejected():
    decision = decide({"resolved_status": "resolved"})

    assert decision.tier == TIER_REJECTED
    assert decision.qualified is False
    assert REJECT_ALREADY_RESOLVED in decision.hard_rejection_codes


def test_promotional_provider_is_rejected():
    decision = decide({"promotional_post": True})

    assert decision.tier == TIER_REJECTED
    assert REJECT_PROMOTIONAL_POST in decision.hard_rejection_codes


def test_personal_consumer_request_is_rejected():
    decision = decide({"personal_request": True})

    assert decision.tier == TIER_REJECTED
    assert REJECT_PERSONAL_REQUEST in decision.hard_rejection_codes


def test_stale_post_is_rejected():
    """A post older than the maximum age is rejected regardless of score."""
    stale_time = NOW - timedelta(days=DEFAULT_MAX_POST_AGE_DAYS + 5)

    decision = evaluate_lead(
        SIGNALS_AT_100,
        suggested_dm="A very specific message.",
        recommended_channel="direct_message",
        post_time=stale_time.isoformat(),
        now=NOW,
    )

    assert decision.tier == TIER_REJECTED
    assert REJECT_STALE_POST in decision.hard_rejection_codes
    assert decision.qualified is False


def test_recent_post_is_not_stale():
    recent = NOW - timedelta(days=3)

    decision = evaluate_lead(
        SIGNALS_AT_100,
        suggested_dm="A very specific message.",
        recommended_channel="direct_message",
        post_time=recent.isoformat(),
        now=NOW,
    )

    assert REJECT_STALE_POST not in decision.hard_rejection_codes
    assert decision.qualified is True


@pytest.mark.parametrize(
    "overrides,expected_code",
    [
        ({"competitor_or_agency": True}, REJECT_COMPETITOR_OR_AGENCY),
        ({"free_only_request": True}, REJECT_FREE_ONLY_REQUEST),
        ({"provider_already_selected": True}, REJECT_PROVIDER_SELECTED),
        ({"spam_risk": "high"}, REJECT_SPAM_OR_AFFILIATE),
        ({"outreach_appropriateness": "inappropriate"},
         REJECT_INAPPROPRIATE_OUTREACH),
        ({"business_context": "none"}, REJECT_NO_BUSINESS_CONTEXT),
        ({"service_categories": []}, REJECT_NO_SERVICE_MATCH),
        ({"service_categories": ["none"]}, REJECT_NO_SERVICE_MATCH),
        ({"disqualifier_codes": [REJECT_JOB_SEEKER]}, REJECT_JOB_SEEKER),
        ({"disqualifier_codes": [REJECT_STUDENT_OR_EDUCATIONAL]},
         REJECT_STUDENT_OR_EDUCATIONAL),
    ],
)
def test_every_hard_rejection_condition(overrides, expected_code):
    decision = decide(overrides)

    assert decision.hard_rejected is True
    assert decision.tier == TIER_REJECTED
    assert decision.qualified is False
    assert expected_code in decision.hard_rejection_codes


def test_rejection_reason_is_human_readable():
    decision = decide({"personal_request": True})

    assert "Personal consumer request" in decision.rejection_reason


# ---------------------------------------------------------------------------
# Manual review behaviour
# ---------------------------------------------------------------------------

def test_manual_review_is_not_qualified_and_has_no_dm():
    decision = decide({"buyer_role": "unknown"})

    assert decision.tier == TIER_MANUAL_REVIEW
    assert decision.manual_review is True
    assert decision.qualified is False
    assert decision.suggested_dm == ""
    assert decision.outreach_ready is False
    assert decision.recommended_channel == "do_not_contact"


def test_manual_review_reason_mentions_manual_review():
    decision = decide({"buyer_role": "unknown"})

    assert "Manual review" in decision.rejection_reason


# ---------------------------------------------------------------------------
# Outreach rules
# ---------------------------------------------------------------------------

def test_no_fallback_dm_is_created():
    """
    A qualifying lead with no AI-written DM must NOT receive a generic one.

    The old pipeline synthesised a template message here, which made
    unqualified-sounding records look outreach-ready.
    """
    decision = decide(suggested_dm="", recommended_channel="direct_message")

    assert decision.qualified is True
    assert decision.suggested_dm == ""
    assert decision.outreach_ready is False
    assert decision.recommended_channel == "do_not_contact"


def test_blank_dm_never_contains_boilerplate():
    decision = decide(suggested_dm="   ", recommended_channel="direct_message")

    assert decision.suggested_dm == ""
    assert "brucetech.ca" not in decision.suggested_dm


def test_do_not_contact_is_never_upgraded():
    """A do_not_contact recommendation must be respected, not overridden."""
    decision = decide(
        suggested_dm="A genuinely specific message about their website.",
        recommended_channel="do_not_contact",
    )

    assert decision.qualified is True
    assert decision.recommended_channel == "do_not_contact"
    assert decision.outreach_ready is False


def test_outreach_ready_requires_dm_and_channel():
    decision = decide()

    assert decision.qualified is True
    assert decision.outreach_ready is True
    assert decision.recommended_channel == "direct_message"
    assert decision.suggested_dm


def test_rejected_lead_has_no_dm():
    decision = decide({"promotional_post": True})

    assert decision.suggested_dm == ""
    assert decision.recommended_channel == "do_not_contact"


# ---------------------------------------------------------------------------
# Prefilter
# ---------------------------------------------------------------------------

def test_prefilter_passes_a_real_request():
    result = prefilter_post(
        "Hi everyone, our Toronto bakery needs a new website with online "
        "ordering. Can anyone recommend a developer who does WordPress and "
        "payment integration? Our current site is broken.",
        post_time=(NOW - timedelta(days=2)).isoformat(),
        now=NOW,
    )

    assert result.passed is True
    assert result.score > 30
    assert result.intent_score > 0


def test_prefilter_rejects_promotional_post():
    result = prefilter_post(
        "I offer website design and SEO services for small businesses. "
        "DM me for a quote, limited time discount available! My services "
        "include WordPress and hosting.",
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
    )

    assert result.passed is False


def test_prefilter_rejects_short_text():
    result = prefilter_post("Need help", now=NOW)

    assert result.passed is False
    assert result.score == 0


def test_prefilter_rejects_stale_post():
    result = prefilter_post(
        "Our business needs a new website and CRM automation, can anyone "
        "recommend a developer in Toronto for this project?",
        post_time=(NOW - timedelta(days=DEFAULT_MAX_POST_AGE_DAYS + 10))
        .isoformat(),
        now=NOW,
    )

    assert result.passed is False
    assert "older than" in " ".join(result.reasons)


def test_prefilter_rejects_off_topic_post():
    result = prefilter_post(
        "Can anyone recommend a good caterer for a birthday party next "
        "weekend? Looking for something affordable in the area please.",
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
    )

    assert result.passed is False


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_missing_signals_do_not_crash():
    decision = evaluate_lead({}, now=NOW)

    assert decision.qualified is False
    assert decision.tier == TIER_REJECTED
    assert decision.lead_score == 0


def test_enum_values_are_case_insensitive():
    decision = decide({"intent_strength": "STRONG"})

    assert decision.lead_score == 65


def test_score_is_clamped_to_100():
    assert calculate_score(SIGNALS_AT_100) <= 100


def test_unparseable_post_time_is_not_treated_as_stale():
    decision = evaluate_lead(
        SIGNALS_AT_100,
        suggested_dm="Specific message.",
        recommended_channel="direct_message",
        post_time="not a date",
        now=NOW,
    )

    assert REJECT_STALE_POST not in decision.hard_rejection_codes
