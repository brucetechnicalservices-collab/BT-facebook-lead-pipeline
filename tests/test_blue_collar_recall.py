"""
Classifier regression tests from the 2026-08-19 Blue Collar Millionaire run.

Apify run BnHEoWoDCZDPnM8iY, dataset MiUEsgX1KSeiGuUyH, 50 posts, 0 OpenAI
calls. Queue coverage is pinned in tests/test_queue_coverage.py; this file
covers the four posts that were evaluated and misclassified.

1. An electrical contractor deliberating over Microsoft 365 workflows for his
   customer intake -- naming microsoft_365, workflow_automation and crm --
   was UNRELATED with NO_BUYING_INTENT. Every implementation pattern required
   a decision already made ("need help setting up"); he was still working out
   how, and deliberation is exactly when a conversation is useful.
2. A surface-restoration startup that could not market or sell had no intent
   at all.
3. A pool builder comparing JobTread, Pool Studio and Vip3D needed to be
   research, and needed to stay out of outreach.
4. A telephony denial-of-service attack against a RingCentral IVR matched no
   BruceTech service. There was no telephony vocabulary in the pipeline.

The precision floor from the same run is at the bottom. Twelve posts that
were correctly rejected and must stay rejected: financing, tax, licensing,
bonuses, business sales, insurance, equity, growth chat, promotions, and
equipment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import intent
from qualification import (
    REJECT_NO_BUYING_INTENT,
    REJECT_NO_SERVICE_MATCH,
    SERVICE_CATEGORIES,
    prefilter_post,
)
from tests.fixtures import (
    BLUE_COLLAR_CANDIDATE_FIXTURES,
    BLUE_COLLAR_NOISE_FIXTURES,
    ELECTRICAL_M365_INTAKE,
    POOL_BUILDER_SOFTWARE_STACK,
    RINGCENTRAL_TELEPHONY_ATTACK,
    SURFACE_RESTORATION_LEAD_GEN,
)

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
MAX_AGE = 5


def screen(text: str, *, days_old: float = 1, comments: int = 3):
    return prefilter_post(
        text,
        post_time=(NOW - timedelta(days=days_old)).isoformat(),
        now=NOW,
        max_post_age_days=MAX_AGE,
        comment_count=comments,
    )


# ---------------------------------------------------------------------------
# Fixture A -- electrical contractor, Microsoft 365, customer intake
# ---------------------------------------------------------------------------

def test_the_m365_intake_post_is_an_implementation_request():
    result = intent.classify_intent(ELECTRICAL_M365_INTAKE)

    assert result.intent_type == intent.INTENT_IMPLEMENTATION_REQUEST
    assert result.is_ai_eligible is True


def test_the_m365_intake_post_reaches_the_ai():
    result = screen(ELECTRICAL_M365_INTAKE)

    assert result.passed is True
    assert REJECT_NO_BUYING_INTENT not in result.rejection_codes
    assert result.rejection_codes == []


def test_the_m365_intake_post_still_matches_the_services_it_named():
    """The service side was never the problem -- the intent was."""
    result = screen(ELECTRICAL_M365_INTAKE)

    assert {"microsoft_365", "workflow_automation", "crm"} <= set(
        result.service_categories
    )
    assert result.match_basis == "named"


@pytest.mark.parametrize(
    "phrase",
    [
        "I am looking at ways to systemize my customer intake",
        "I'm considering setting up some workflows and forms through that",
        "looking at ways to streamline our job intake",
        "want to systemize my service requests",
        "is there a more streamline/professional way to handle all of this",
    ],
)
def test_deliberation_phrasings_are_implementation_requests(phrase):
    """The exact phrasings named in the live review."""
    post = f"As a small electrical contractor, {phrase} for our WordPress site."

    assert intent.classify_intent(post).intent_type in {
        intent.INTENT_IMPLEMENTATION_REQUEST,
        intent.INTENT_PROVIDER_REQUEST,
    }


def test_isolated_workflow_words_are_not_an_implementation_request():
    """
    "Do not make isolated forms, workflow, or system mentions sufficient."
    """
    for post in (
        "Filled out a bunch of forms today, what a workflow. Anyway.",
        "The system was down at the parts counter again this morning.",
        "New workflow at the shop is going great, just sharing a win.",
    ):
        assert intent.classify_intent(post).intent_type != (
            intent.INTENT_IMPLEMENTATION_REQUEST
        )


def test_deliberation_needs_business_context():
    """Operator framing is required, exactly as for the other inferred paths."""
    anonymous = (
        "Looking at ways to systemize customer intake, considering setting up "
        "some workflows and forms through Microsoft 365."
    )

    assert intent.operator_context_terms(anonymous) == []
    assert intent.classify_intent(anonymous).intent_type != (
        intent.INTENT_IMPLEMENTATION_REQUEST
    )


def test_deliberation_about_something_unrelated_still_fails_on_service():
    """Intent is not a service match. Both are still required."""
    post = (
        "As a small contractor I'm looking at ways to systemize my hiring and "
        "employee onboarding paperwork this year."
    )
    result = screen(post)

    assert result.service_matched is False
    assert result.passed is False


# ---------------------------------------------------------------------------
# Fixture B -- surface restoration, cannot market or sell
# ---------------------------------------------------------------------------

def test_the_surface_restoration_post_has_a_qualifying_intent():
    result = intent.classify_intent(SURFACE_RESTORATION_LEAD_GEN)

    assert result.intent_type in {
        intent.INTENT_BUSINESS_PAIN,
        intent.INTENT_PROVIDER_REQUEST,
        intent.INTENT_IMPLEMENTATION_REQUEST,
    }
    assert result.is_ai_eligible is True


def test_the_surface_restoration_post_reaches_the_ai_as_adjacent_growth():
    result = screen(SURFACE_RESTORATION_LEAD_GEN)

    assert result.passed is True
    assert result.match_basis == "adjacent"
    assert REJECT_NO_SERVICE_MATCH not in result.rejection_codes


def test_the_adjacent_match_still_defers_to_the_model():
    services = intent.match_services(SURFACE_RESTORATION_LEAD_GEN)

    assert services.needs_ai_confirmation is True
    assert services.strong_terms == []


def test_growth_pain_alone_is_not_an_intent():
    """
    All three are required: growth pain, an adjacent-growth request, and
    operator context.
    """
    just_pain = (
        "I own a small contracting company and selling is new to me, I don't "
        "know where to start with any of it."
    )

    assert intent.growth_pain_evidence(just_pain) != []
    assert intent.adjacent_growth_evidence(just_pain) == []
    assert screen(just_pain).passed is False


def test_a_general_growth_question_is_not_growth_pain():
    from tests.fixtures import GENERAL_GROWTH_QUESTION

    assert screen(GENERAL_GROWTH_QUESTION).passed is False


# ---------------------------------------------------------------------------
# Fixture C -- pool builder software stack
# ---------------------------------------------------------------------------

def test_the_pool_software_post_is_tool_research():
    assert intent.classify_intent(POOL_BUILDER_SOFTWARE_STACK).intent_type == (
        intent.INTENT_TOOL_RESEARCH
    )


def test_the_pool_software_post_never_produces_outreach():
    """Useful operator intelligence. Not a person to cold-DM."""
    result = screen(POOL_BUILDER_SOFTWARE_STACK)

    assert result.allows_outreach is False
    assert intent.INTENT_TOOL_RESEARCH not in intent.OUTREACH_INTENTS


def test_tool_research_outranks_the_deliberation_path():
    """
    "Launching a new division and trying to get our software stack sorted" is
    deliberation too. Asking which product to buy is still research.
    """
    result = intent.classify_intent(POOL_BUILDER_SOFTWARE_STACK)

    assert result.intent_type == intent.INTENT_TOOL_RESEARCH


# ---------------------------------------------------------------------------
# Fixture D -- RingCentral telephony attack
# ---------------------------------------------------------------------------

def test_the_telephony_attack_post_is_business_pain():
    assert intent.classify_intent(RINGCENTRAL_TELEPHONY_ATTACK).intent_type == (
        intent.INTENT_BUSINESS_PAIN
    )


def test_the_telephony_attack_post_matches_a_credible_service():
    result = screen(RINGCENTRAL_TELEPHONY_ATTACK)

    assert result.service_matched is True
    assert "business_telephony" in result.service_categories
    assert result.passed is True


def test_business_telephony_is_a_category_the_model_can_return():
    assert "business_telephony" in SERVICE_CATEGORIES
    assert set(intent.SERVICE_CATEGORY_NAMES) <= set(SERVICE_CATEGORIES)


@pytest.mark.parametrize(
    "term",
    ["RingCentral", "IVR", "SIP trunk", "VoIP", "PBX", "auto attendant"],
)
def test_business_phone_platforms_match_as_a_service(term):
    post = f"Our {term} setup keeps dropping calls and we need it fixed."

    assert intent.match_services(post).matched is True


def test_a_consumer_spam_call_complaint_is_not_a_lead():
    """
    Found by an adversarial sweep before shipping: "spam calls" and
    "robocall" are consumer words. They corroborate a business phone problem;
    they do not establish one.
    """
    consumer = (
        "My personal phone keeps getting spam calls, anyone know how to stop "
        "this? Not a business thing just annoying."
    )
    result = screen(consumer)

    assert result.service_matched is False
    assert result.passed is False


def test_the_telephony_post_is_not_automatically_qualified():
    """
    It reaches the model for a fit determination. That is all. If the model
    finds no BruceTech service in it, NO_SERVICE_MATCH still rejects it --
    reaching the AI is permission to ask, never a verdict.
    """
    from qualification import evaluate_lead

    assert screen(RINGCENTRAL_TELEPHONY_ATTACK).passed is True

    unconvinced = evaluate_lead(
        {
            "intent_strength": "moderate",
            "service_categories": [],
            "business_context": "small_business",
            "classification_confidence": 0.8,
        },
        suggested_dm="Hi, saw your phone system post.",
        recommended_channel="direct_message",
        intent_type=intent.INTENT_BUSINESS_PAIN,
        now=NOW,
    )

    assert REJECT_NO_SERVICE_MATCH in unconvinced.hard_rejection_codes
    assert unconvinced.qualified is False
    assert unconvinced.outreach_ready is False


# ---------------------------------------------------------------------------
# The precision floor from the same run
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name, text",
    sorted(BLUE_COLLAR_NOISE_FIXTURES.items()),
)
def test_every_correctly_rejected_blue_collar_post_stays_rejected(name, text):
    assert screen(text).passed is False, f"{name} would now reach the model"


@pytest.mark.parametrize(
    "name, text",
    sorted(BLUE_COLLAR_CANDIDATE_FIXTURES.items()),
)
def test_every_blue_collar_candidate_reaches_the_ai(name, text):
    assert screen(text).passed is True, f"{name} still does not reach the model"


def test_the_blue_collar_split_is_exactly_the_documented_one():
    reaching = {
        name
        for name, text in {
            **BLUE_COLLAR_CANDIDATE_FIXTURES,
            **BLUE_COLLAR_NOISE_FIXTURES,
        }.items()
        if screen(text).passed
    }

    assert reaching == set(BLUE_COLLAR_CANDIDATE_FIXTURES)


def test_a_stale_blue_collar_lead_still_never_reaches_the_ai():
    result = screen(ELECTRICAL_M365_INTAKE, days_old=17)

    assert result.passed is False
    assert result.is_stale is True
