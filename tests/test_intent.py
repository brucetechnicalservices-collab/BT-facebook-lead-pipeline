"""
Regression tests for deterministic intent classification and service matching.

The headline case is ``test_working_capital_does_not_match_api``: a plain
substring match let "working capital" register as an API integration lead, and
that single false positive is what sent excavation-financing questions to the
model. Every service term is now word-boundary anchored.

These tests import only ``intent`` and ``qualification``, so they run with no
credentials, no network, and no Airtable base.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import intent
from qualification import (
    REJECT_FUNDING_REQUEST,
    REJECT_HIRING_UNRELATED,
    REJECT_NO_BUYING_INTENT,
    TIER_REJECTED,
    evaluate_lead,
    prefilter_post,
)
from tests.fixtures import (
    API_INTEGRATION,
    CRM_RESEARCH,
    EXCAVATION_FUNDING,
    GOHIGHLEVEL_PROVIDER,
    JOB_SEEKER,
    OFFICE_NETWORK,
    PEN_AND_PAPER_ELECTRICIAN,
    PROMOTIONAL_AGENCY,
    RESOLVED_REQUEST,
    VIRTUAL_ASSISTANT,
)

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


def fresh(days: float = 1.0) -> str:
    return (NOW - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# The api / capital regression
# ---------------------------------------------------------------------------

def test_working_capital_does_not_match_api():
    """
    THE regression. "working capital" contains the substring "api".

    Before word-boundary matching this returned a service match and an
    excavation-financing post was sent to the model as an integration lead.
    """
    assert intent.match_services("we need working capital").matched is False


def test_api_token_still_matches_when_it_is_a_real_word():
    """The fix must not cost us genuine API leads."""
    services = intent.match_services(API_INTEGRATION)

    assert services.matched is True
    assert "api" in services.strong_terms
    assert "integrations" in services.categories


@pytest.mark.parametrize(
    "text",
    [
        "working capital",
        "raising capital this year",
        "capital expenditure",
        "Capital One business card",
    ],
)
def test_capital_never_matches_api(text):
    assert "api" not in intent.match_services(text).strong_terms


@pytest.mark.parametrize(
    "text,term",
    [
        ("we are hiring a position", "pos"),
        ("it is not possible right now", "pos"),
        ("please email me", "ai"),
        ("said that yesterday", "ai"),
        ("sitting in a chair", "ai"),
        ("a seoul trip", "seo"),
    ],
)
def test_short_tokens_do_not_match_inside_words(text, term):
    """Short technical tokens are the whole reason for word boundaries."""
    services = intent.match_services(text)

    assert term not in services.strong_terms
    assert term not in services.weak_terms


def test_compile_term_is_word_boundary_anchored():
    pattern = intent.compile_term("api")

    assert pattern.search("integrating an API") is not None
    assert pattern.search("working capital") is None


def test_compile_term_matches_plurals_and_flexible_whitespace():
    assert intent.compile_term("backup").search("our backups failed")
    assert intent.compile_term("microsoft 365").search("Microsoft  365")


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        # Required regression cases, verbatim from the specification.
        ("Looking for someone to set up our CRM",
         intent.INTENT_PROVIDER_REQUEST),
        ("We need help connecting our booking system to payments",
         intent.INTENT_IMPLEMENTATION_REQUEST),
        ("What CRM does everyone use?", intent.INTENT_TOOL_RESEARCH),
        ("How do I finance another truck?", intent.INTENT_GENERAL_ADVICE),
        # Production fixtures.
        (EXCAVATION_FUNDING, intent.INTENT_GENERAL_ADVICE),
        (GOHIGHLEVEL_PROVIDER, intent.INTENT_PROVIDER_REQUEST),
        (PEN_AND_PAPER_ELECTRICIAN, intent.INTENT_BUSINESS_PAIN),
        (VIRTUAL_ASSISTANT, intent.INTENT_UNRELATED),
        (CRM_RESEARCH, intent.INTENT_TOOL_RESEARCH),
        # Other general-advice shapes.
        ("Should I hire an employee or stay solo?",
         intent.INTENT_GENERAL_ADVICE),
        ("How much inventory should I buy for spring?",
         intent.INTENT_GENERAL_ADVICE),
        ("How should I price my services this year?",
         intent.INTENT_GENERAL_ADVICE),
    ],
)
def test_intent_is_classified(text, expected):
    assert intent.classify_intent(text).intent_type == expected


def test_provider_request_beats_implementation_when_both_present():
    """
    "Looking for someone to set up our CRM" is both. Provider wins: the
    author has asked to hire, which is the more valuable signal.
    """
    result = intent.classify_intent("Looking for someone to set up our CRM")

    assert result.intent_type == intent.INTENT_PROVIDER_REQUEST
    assert result.allows_outreach is True


def test_tool_research_is_not_an_outreach_intent():
    result = intent.classify_intent(CRM_RESEARCH)

    assert result.intent_type == intent.INTENT_TOOL_RESEARCH
    assert result.is_ai_eligible is True
    assert result.allows_outreach is False


def test_general_advice_is_not_ai_eligible():
    result = intent.classify_intent(EXCAVATION_FUNDING)

    assert result.is_ai_eligible is False
    assert result.allows_outreach is False


def test_empty_text_is_unrelated():
    assert intent.classify_intent("").intent_type == intent.INTENT_UNRELATED
    assert intent.classify_intent(None).intent_type == intent.INTENT_UNRELATED


# ---------------------------------------------------------------------------
# Service matching
# ---------------------------------------------------------------------------

def test_weak_term_alone_is_not_a_service_match():
    """"Grow my network" is not a managed IT lead."""
    services = intent.match_services(
        "Trying to grow my network and meet more people in the trades."
    )

    assert services.matched is False


def test_weak_term_with_technical_context_is_a_match():
    services = intent.match_services(OFFICE_NETWORK)

    assert services.matched is True
    assert services.has_tech_context is True


def test_two_weak_categories_corroborate_each_other():
    services = intent.match_services(
        "Our payment software and our customer database do not talk to "
        "each other at all."
    )

    assert services.matched is True


def test_security_deposit_is_not_a_cybersecurity_lead():
    services = intent.match_services(
        "Does anyone know if a security deposit is normal for a "
        "commercial lease agreement in this area?"
    )

    assert services.matched is False


def test_strong_term_alone_is_enough():
    services = intent.match_services("Our WordPress site needs work.")

    assert services.matched is True
    assert services.strong is True


# ---------------------------------------------------------------------------
# Negative signals
# ---------------------------------------------------------------------------

def test_promotional_language_is_detected():
    negatives = intent.detect_negative_signals(PROMOTIONAL_AGENCY)

    assert negatives.promotional
    assert negatives.any_present is True


def test_job_seeking_language_is_detected():
    assert intent.detect_negative_signals(JOB_SEEKER).job_seeking


def test_resolved_language_is_detected():
    assert intent.detect_negative_signals(RESOLVED_REQUEST).resolved


def test_commercial_context_is_detected():
    assert intent.commercial_context_terms(PEN_AND_PAPER_ELECTRICIAN)


@pytest.mark.parametrize(
    "text,expected",
    [
        (EXCAVATION_FUNDING, "FUNDING"),
        ("Should I hire an employee this year?", "HIRING"),
        ("What is everyone's favourite coffee shop?", None),
    ],
)
def test_general_advice_subtype(text, expected):
    assert intent.general_advice_subtype(text) == expected


# ---------------------------------------------------------------------------
# Prefilter: the five production fixtures end to end
# ---------------------------------------------------------------------------

def test_excavation_funding_never_reaches_the_ai():
    """Example A: the post that motivated this release."""
    result = prefilter_post(
        EXCAVATION_FUNDING, post_time=fresh(1), now=NOW, comment_count=4
    )

    assert result.passed is False
    assert result.service_matched is False
    assert result.intent_type == intent.INTENT_GENERAL_ADVICE


def test_gohighlevel_request_reaches_the_ai_with_a_strong_score():
    """Example B: a genuine provider request."""
    result = prefilter_post(
        GOHIGHLEVEL_PROVIDER, post_time=fresh(0.5), now=NOW, comment_count=2
    )

    assert result.passed is True
    assert result.intent_type == intent.INTENT_PROVIDER_REQUEST
    assert result.is_provider_request is True
    assert result.score >= 60


def test_business_pain_reaches_the_ai():
    """Example C: real pain, real service fit, but no explicit request."""
    result = prefilter_post(
        PEN_AND_PAPER_ELECTRICIAN,
        post_time=fresh(1),
        now=NOW,
        comment_count=6,
    )

    assert result.passed is True
    assert result.intent_type == intent.INTENT_BUSINESS_PAIN
    assert result.service_matched is True


def test_virtual_assistant_request_is_not_a_brucetech_lead():
    """Example D: no solution hopping. Not our service, not our lead."""
    result = prefilter_post(
        VIRTUAL_ASSISTANT, post_time=fresh(1), now=NOW, comment_count=1
    )

    assert result.passed is False
    assert result.service_matched is False


def test_tool_research_reaches_the_ai_but_never_outreach():
    """Example E: manual review at most."""
    result = prefilter_post(
        CRM_RESEARCH, post_time=fresh(1), now=NOW, comment_count=12
    )

    assert result.passed is True
    assert result.intent_type == intent.INTENT_TOOL_RESEARCH
    assert result.allows_outreach is False


def test_tool_research_can_be_excluded_from_the_ai_entirely():
    result = prefilter_post(
        CRM_RESEARCH,
        post_time=fresh(1),
        now=NOW,
        allow_tool_research=False,
    )

    assert result.passed is False


# ---------------------------------------------------------------------------
# Prefilter gates
# ---------------------------------------------------------------------------

def test_service_signal_is_required_before_ai_eligibility():
    """Requirement 19: no service match, no AI call, whatever the intent."""
    result = prefilter_post(
        "Looking for someone reliable to help me move house next weekend, "
        "happy to pay a fair rate for a couple of hours of work.",
        post_time=fresh(1),
        now=NOW,
    )

    assert result.intent_type == intent.INTENT_PROVIDER_REQUEST
    assert result.service_matched is False
    assert result.passed is False


def test_promotional_provider_post_does_not_reach_the_ai():
    """Requirement 20: a provider advertising itself is not a buyer."""
    result = prefilter_post(
        PROMOTIONAL_AGENCY, post_time=fresh(1), now=NOW
    )

    assert result.passed is False
    assert result.promotional is True


def test_job_seeker_does_not_reach_the_ai():
    result = prefilter_post(JOB_SEEKER, post_time=fresh(1), now=NOW)

    assert result.passed is False


def test_resolved_request_does_not_reach_the_ai():
    result = prefilter_post(RESOLVED_REQUEST, post_time=fresh(1), now=NOW)

    assert result.passed is False


def test_fresh_posts_outscore_stale_ones():
    fresh_result = prefilter_post(
        GOHIGHLEVEL_PROVIDER, post_time=fresh(0.5), now=NOW
    )
    older_result = prefilter_post(
        GOHIGHLEVEL_PROVIDER, post_time=fresh(4), now=NOW
    )

    assert fresh_result.score > older_result.score


def test_comment_competition_lowers_the_score_but_does_not_reject():
    quiet = prefilter_post(
        GOHIGHLEVEL_PROVIDER, post_time=fresh(1), now=NOW, comment_count=2
    )
    busy = prefilter_post(
        GOHIGHLEVEL_PROVIDER, post_time=fresh(1), now=NOW, comment_count=80
    )

    assert busy.score < quiet.score
    # A strong lead is never hard-rejected on comment volume alone.
    assert busy.passed is True


def test_prefilter_breakdown_is_documented_and_complete():
    result = prefilter_post(
        GOHIGHLEVEL_PROVIDER, post_time=fresh(1), now=NOW, comment_count=3
    )

    assert set(result.breakdown) == {
        "intent",
        "service_match",
        "freshness",
        "commercial_context",
        "length",
        "competition",
        "penalties",
    }
    assert result.score == max(0, min(sum(result.breakdown.values()), 100))


# ---------------------------------------------------------------------------
# Intent vetoes at decision time
# ---------------------------------------------------------------------------

STRONG_SIGNALS = {
    "intent_strength": "strong",
    "service_categories": ["website_development", "crm"],
    "problem_specificity": "detailed",
    "business_context": "small_business",
    "purchase_signal": "ready_to_buy",
    "urgency": "medium",
    "business_impact": "medium",
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
}

SPECIFIC_DM = (
    "Hi Sam, saw your post about rebuilding the booking flow. We do this "
    "at brucetech.ca. Want to compare notes?"
)


def decide(**kwargs):
    params = {
        "suggested_dm": SPECIFIC_DM,
        "recommended_channel": "direct_message",
        "now": NOW,
        **kwargs,
    }
    return evaluate_lead(STRONG_SIGNALS, **params)


def test_general_advice_intent_vetoes_a_perfect_score():
    """Requirement 16: a financing question is never a sales lead."""
    decision = decide(
        intent_type=intent.INTENT_GENERAL_ADVICE,
        post_text=EXCAVATION_FUNDING,
    )

    assert decision.lead_score >= 80
    assert decision.tier == TIER_REJECTED
    assert decision.qualified is False
    assert REJECT_FUNDING_REQUEST in decision.hard_rejection_codes


def test_hiring_question_gets_its_own_code():
    decision = decide(
        intent_type=intent.INTENT_GENERAL_ADVICE,
        post_text="Should I hire an employee or stay solo this year?",
    )

    assert REJECT_HIRING_UNRELATED in decision.hard_rejection_codes


def test_unrelated_intent_gets_the_generic_code():
    decision = decide(
        intent_type=intent.INTENT_UNRELATED,
        post_text="Beautiful morning out there everyone.",
    )

    assert REJECT_NO_BUYING_INTENT in decision.hard_rejection_codes


def test_tool_research_qualifies_but_is_never_outreach_ready():
    """Requirement 15 and section 11: research goes to a human, not a DM."""
    decision = decide(intent_type=intent.INTENT_TOOL_RESEARCH)

    assert decision.qualified is True
    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""


def test_provider_request_can_be_outreach_ready():
    decision = decide(intent_type=intent.INTENT_PROVIDER_REQUEST)

    assert decision.outreach_ready is True
    assert decision.suggested_dm == SPECIFIC_DM


def test_implementation_request_can_be_outreach_ready():
    decision = decide(intent_type=intent.INTENT_IMPLEMENTATION_REQUEST)

    assert decision.outreach_ready is True


def test_business_pain_needs_a_higher_score_for_outreach():
    """Business pain is inferred, not requested, so the bar is higher."""
    strong = decide(intent_type=intent.INTENT_BUSINESS_PAIN)
    assert strong.outreach_ready is True

    # Scores 68: qualified, but short of the 70-point business-pain bar.
    weaker = evaluate_lead(
        {**STRONG_SIGNALS, "urgency": "none", "business_impact": "none",
         "purchase_signal": "researching", "location": "unknown",
         "buyer_role": "employee", "problem_specificity": "specific",
         "business_context": "individual_or_sole_trader"},
        suggested_dm=SPECIFIC_DM,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_BUSINESS_PAIN,
    )

    assert weaker.qualified is True
    assert 65 <= weaker.lead_score < 70
    assert weaker.outreach_ready is False
    # Still qualified, so a human can pick it up -- just not auto-contacted.
    assert weaker.suggested_dm == ""


def test_low_confidence_blocks_outreach_but_not_qualification():
    decision = evaluate_lead(
        {**STRONG_SIGNALS, "classification_confidence": 0.4},
        suggested_dm=SPECIFIC_DM,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        min_outreach_confidence=0.55,
    )

    assert decision.qualified is True
    assert decision.outreach_ready is False


def test_outreach_ready_is_strictly_narrower_than_qualified():
    """Section 11: the two are not the same question."""
    decision = decide(
        intent_type=intent.INTENT_TOOL_RESEARCH,
    )

    assert decision.qualified is True
    assert decision.outreach_ready is False


def test_intent_is_optional_so_legacy_callers_are_unaffected():
    decision = decide()

    assert decision.qualified is True
    assert decision.outreach_ready is True
