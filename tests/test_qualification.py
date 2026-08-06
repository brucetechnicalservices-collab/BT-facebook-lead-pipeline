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
    KNOWN_DISQUALIFIER_CODES,
    PLATFORM_CONVERSION_BONUS,
    REJECT_ALREADY_RESOLVED,
    REJECT_COMPETITOR_OR_AGENCY,
    REJECT_FREE_ONLY_REQUEST,
    REJECT_INAPPROPRIATE_OUTREACH,
    REJECT_JOB_SEEKER,
    REJECT_NO_BUSINESS_CONTEXT,
    REJECT_NO_SERVICE_MATCH,
    REJECT_NO_WEBSITE_OPPORTUNITY,
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
    WEBSITE_FOCUS_MAX_BONUS,
    calculate_score,
    evaluate_lead,
    prefilter_post,
    score_signals,
    website_opportunity_type,
    website_platform,
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
# Website analysis focus
# ---------------------------------------------------------------------------

def test_focus_mode_rejects_a_post_with_no_website_need():
    """A perfect managed-IT lead is not a website lead."""
    decision = decide(
        {
            **SIGNALS_AT_100,
            "service_categories": ["managed_it", "microsoft_365"],
            "website_opportunity": "none",
        },
        website_focus=True,
    )

    assert decision.tier == TIER_REJECTED
    assert decision.qualified is False
    assert REJECT_NO_WEBSITE_OPPORTUNITY in decision.hard_rejection_codes


def test_the_same_post_is_unaffected_outside_focus_mode():
    decision = decide(
        {
            **SIGNALS_AT_100,
            "service_categories": ["managed_it", "microsoft_365"],
            "website_opportunity": "none",
        }
    )

    assert REJECT_NO_WEBSITE_OPPORTUNITY not in decision.hard_rejection_codes
    assert decision.qualified is True


def test_focus_mode_keeps_a_website_service_match():
    decision = decide(
        {"service_categories": ["website_development"]},
        website_focus=True,
    )

    assert REJECT_NO_WEBSITE_OPPORTUNITY not in decision.hard_rejection_codes


def test_a_website_opportunity_alone_survives_focus_mode():
    """The category may be generic if the opportunity is explicit."""
    decision = decide(
        {
            "service_categories": ["business_process_consulting"],
            "website_opportunity": "no_website",
        },
        website_focus=True,
    )

    assert REJECT_NO_WEBSITE_OPPORTUNITY not in decision.hard_rejection_codes


def test_focus_mode_lifts_an_expensive_platform_lead():
    """Shopify cost pain plus the platform itself is the strongest offer."""
    base = decide({"website_opportunity": "none"}, website_focus=True)
    lifted = decide(
        {
            "website_opportunity": "expensive_platform",
            "website_platform": "shopify",
        },
        website_focus=True,
    )

    assert lifted.lead_score == min(
        base.lead_score + WEBSITE_FOCUS_MAX_BONUS, 100
    )
    assert lifted.lead_score - base.lead_score == WEBSITE_FOCUS_MAX_BONUS
    assert lifted.tier in (TIER_QUALIFIED, TIER_HOT)


def test_the_focus_bonus_can_promote_a_manual_review_lead():
    """The point of the bonus: a borderline website lead reaches a human."""
    borderline = {"problem_specificity": "specific", "purchase_signal": "none"}

    assert decide(borderline).tier == TIER_MANUAL_REVIEW

    promoted = decide(
        {**borderline, "website_opportunity": "outdated_website"},
        website_focus=True,
    )

    assert promoted.tier == TIER_QUALIFIED
    assert promoted.qualified is True


def test_the_focus_bonus_is_capped():
    breakdown = score_signals(
        signals(
            website_opportunity="expensive_platform",
            website_platform="wix",
        ),
        website_focus=True,
    )

    assert breakdown["website_focus"] == WEBSITE_FOCUS_MAX_BONUS


def test_no_focus_bonus_without_focus_mode():
    assert calculate_score(
        signals(
            website_opportunity="expensive_platform",
            website_platform="shopify",
        )
    ) == calculate_score(signals())


def test_wordpress_gets_no_conversion_bonus():
    """There is no monthly plan to replace on WordPress."""
    hosted = calculate_score(
        signals(
            website_opportunity="redesign_or_refresh",
            website_platform="squarespace",
        ),
        website_focus=True,
    )
    self_hosted = calculate_score(
        signals(
            website_opportunity="redesign_or_refresh",
            website_platform="wordpress",
        ),
        website_focus=True,
    )

    assert hosted - self_hosted == PLATFORM_CONVERSION_BONUS


def test_an_unknown_opportunity_value_is_treated_as_none():
    assert website_opportunity_type({"website_opportunity": "nonsense"}) == "none"
    assert website_platform({"website_platform": "nonsense"}) == "unknown"


def test_the_model_cannot_raise_the_focus_rejection_itself():
    """NO_WEBSITE_OPPORTUNITY is derived in Python, never accepted."""
    decision = decide(
        {"disqualifier_codes": [REJECT_NO_WEBSITE_OPPORTUNITY]},
    )

    assert REJECT_NO_WEBSITE_OPPORTUNITY not in decision.hard_rejection_codes
    assert REJECT_NO_WEBSITE_OPPORTUNITY not in KNOWN_DISQUALIFIER_CODES


def test_a_hard_rejection_still_beats_a_strong_website_lead():
    decision = decide(
        {
            **SIGNALS_AT_100,
            "website_opportunity": "expensive_platform",
            "website_platform": "shopify",
            "competitor_or_agency": True,
        },
        website_focus=True,
    )

    assert decision.tier == TIER_REJECTED
    assert REJECT_COMPETITOR_OR_AGENCY in decision.hard_rejection_codes


# ---------------------------------------------------------------------------
# Website-focus prefilter
# ---------------------------------------------------------------------------

SHOPIFY_COST_POST = (
    "We run a small furniture shop and our Shopify plan plus apps is costing "
    "us almost $400 a month now. The monthly fees are getting hard to "
    "justify. Is there a cheaper option that still handles our online store?"
)

NO_WEBSITE_POST = (
    "We've been running our catering business off this Facebook page for "
    "three years and we don't have a website at all. Customers keep asking "
    "for one so they can see the menu and book. Where do we even start?"
)

MANAGED_IT_POST = (
    "Can anyone recommend an IT company in Toronto? We need help migrating "
    "our team's email to Microsoft 365 and setting up backups for our "
    "office network before the end of the month."
)


def test_focus_prefilter_passes_a_platform_cost_post():
    result = prefilter_post(
        SHOPIFY_COST_POST,
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
        website_focus=True,
    )

    assert result.passed is True
    assert result.website_score > 0


def test_focus_prefilter_passes_a_business_with_no_website():
    """No 'looking for' language, but the opportunity is unmistakable."""
    result = prefilter_post(
        NO_WEBSITE_POST,
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
        website_focus=True,
    )

    assert result.passed is True


def test_focus_prefilter_rejects_a_managed_it_post():
    result = prefilter_post(
        MANAGED_IT_POST,
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
        website_focus=True,
    )

    assert result.passed is False
    assert "No website keywords detected." in result.reasons


def test_the_same_it_post_passes_without_focus_mode():
    result = prefilter_post(
        MANAGED_IT_POST,
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
    )

    assert result.passed is True


def test_focus_prefilter_still_rejects_promotional_website_posts():
    result = prefilter_post(
        "I offer website redesign and SEO services for small businesses. "
        "DM me for a quote, limited time discount available! My services "
        "include WordPress migration and hosting.",
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
        website_focus=True,
    )

    assert result.passed is False


def test_website_score_is_zero_outside_focus_mode():
    result = prefilter_post(
        SHOPIFY_COST_POST,
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
    )

    assert result.website_score == 0


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
