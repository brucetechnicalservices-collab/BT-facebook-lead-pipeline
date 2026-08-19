"""
Recall regression tests from the 2026-08-19 fresh-run medical spa scrape.

Apify run Q3Ix6zmHrEDhgiQGf, dataset rVjOEFf81ZIT23RWl, 50 posts. Attribution
and scraper-run logging worked correctly. Qualification did not: effectively
nothing reached the model, including several credible business problems and
two explicit provider requests.

Four recall defects, each reproduced here against the real post:

1. A med spa manager with "200 units missing" and "no systems in place" was
   UNRELATED with no service match. Operational failure described in plain
   English is still operational failure.
2. "We offer facials, DiamondGlow, SkinPen" made a post PROMOTIONAL_POST.
   Describing what your business sells is not advertising it.
3. "Looking for a marketing agency to get me patients" matched no BruceTech
   service, though BruceTech delivers exactly that through the site, SEO,
   booking flow and CRM underneath it.
4. "Mangomint or boulevard POS system and why?" was UNRELATED rather than
   TOOL_RESEARCH.

The second half of this file is the counterweight. A medical spa group talks
about buying lasers, syringes, and treatment chairs constantly, and none of
that is BruceTech work. Every widening above is paired with a test that the
noise still stops for free.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import intent
from qualification import (
    REJECT_NO_BUYING_INTENT,
    REJECT_NO_SERVICE_MATCH,
    REJECT_PROMOTIONAL_POST,
    prefilter_post,
)
from tests.fixtures import (
    AESTHETIC_CHAIR_FOR_SALE,
    BOULEVARD_SWITCHING_RESEARCH,
    GOHIGHLEVEL_PROVIDER,
    INFLUENCER_ONLY_REQUEST,
    INJECTOR_HIRING,
    LASER_DEVICE_PURCHASE,
    MANGOMINT_OR_BOULEVARD,
    MEDSPA_CANDIDATE_FIXTURES,
    MEDSPA_ESTHETICIAN_SCHEDULE,
    MEDSPA_INVENTORY_NO_SYSTEMS,
    MEDSPA_NOISE_FIXTURES,
    NEW_MEDSPA_MARKETING_COMPANY,
    SOLO_MD_MARKETING_AGENCY,
    WEB_DESIGN_SELLER_CTA,
)

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
MAX_AGE = 5


def screen(text: str, *, days_old: float = 1, comments: int = 3):
    """Run a post through the real pre-AI screen."""
    return prefilter_post(
        text,
        post_time=(NOW - timedelta(days=days_old)).isoformat(),
        now=NOW,
        max_post_age_days=MAX_AGE,
        comment_count=comments,
    )


# ---------------------------------------------------------------------------
# Test 1 -- med spa inventory / no systems in place
# ---------------------------------------------------------------------------

def test_medspa_inventory_post_is_business_pain():
    result = intent.classify_intent(MEDSPA_INVENTORY_NO_SYSTEMS)

    assert result.intent_type == intent.INTENT_BUSINESS_PAIN
    assert result.is_ai_eligible is True


def test_medspa_inventory_post_reaches_the_ai():
    result = screen(MEDSPA_INVENTORY_NO_SYSTEMS)

    assert result.passed is True
    assert result.service_matched is True
    assert result.rejection_codes == []


def test_medspa_inventory_match_is_recorded_as_described_not_named():
    """
    The author never says "software". The audit trail must not imply they
    did, or an operator reading Airtable will think the post named a
    requirement it never named.
    """
    result = screen(MEDSPA_INVENTORY_NO_SYSTEMS)

    assert result.match_basis == "described"
    assert "business_process_consulting" in result.service_categories


def test_operational_pain_needs_business_context():
    """
    The same failure without a business behind it is a consumer grumble.
    """
    consumer = (
        "I have no systems in place at home and I'm missing about 200 of my "
        "sons lego pieces, nobody can tell me where they went honestly."
    )

    assert screen(consumer).passed is False


@pytest.mark.parametrize(
    "text",
    [
        # Found by an adversarial sweep of the recall widening, before it
        # shipped: "salon" is commercial context, "double booked" is
        # operational pain, and the author is a customer.
        "My salon appointment was double booked and nobody could tell me "
        "where my order went, so frustrating as a customer honestly.",
        "I went to a med spa and they had no systems in place, my products "
        "went missing and nobody could tell me where they went.",
        "The clinic I go to has no process in place, they lost my paperwork "
        "and nobody could tell me where it went. Really frustrating.",
    ],
)
def test_a_customer_describing_the_same_failure_is_not_a_lead(text):
    """
    Naming a business is not running one. The inferred paths gate on
    operator framing -- "I own", "owner here", "new manager at" -- and not on
    the bare industry nouns that carry scoring weight.

    This governs the *inferred* paths only. A post that names a BruceTech
    service outright ("no booking system in place") still matches through the
    long-standing ``named`` path whoever wrote it, which this change
    deliberately leaves alone.
    """
    result = screen(text)

    assert result.service_matched is False
    assert result.match_basis == "none"
    assert result.passed is False


def test_operator_context_is_narrower_than_commercial_context():
    customer = "My salon appointment was double booked again."

    assert intent.commercial_context_terms(customer) != []
    assert intent.operator_context_terms(customer) == []


def test_the_word_inventory_alone_is_not_enough():
    """The rule is operational *failure*, not the noun 'inventory'."""
    chatter = (
        "I own a small salon and our inventory arrived today, so excited to "
        "show everyone the new retail shelf we put together this weekend."
    )

    result = screen(chatter)

    assert result.passed is False
    assert intent.operations_pain_evidence(chatter) == []


# ---------------------------------------------------------------------------
# Test 2 and 3 -- "we offer" as context versus as advertising
# ---------------------------------------------------------------------------

def test_we_offer_as_context_is_not_promotional():
    signals = intent.detect_negative_signals(MEDSPA_ESTHETICIAN_SCHEDULE)

    assert signals.promotional == []
    # Still recorded, so the diagnostics show what was considered.
    assert "we offer" in signals.self_description


def test_the_medspa_esthetician_post_reaches_the_ai():
    result = screen(MEDSPA_ESTHETICIAN_SCHEDULE)

    assert REJECT_PROMOTIONAL_POST not in result.rejection_codes
    assert result.passed is True


def test_we_offer_with_a_call_to_action_is_promotional():
    signals = intent.detect_negative_signals(WEB_DESIGN_SELLER_CTA)

    assert signals.promotional != []
    assert "we offer" in signals.promotional


def test_the_seller_post_never_reaches_the_ai():
    result = screen(WEB_DESIGN_SELLER_CTA)

    assert result.passed is False
    assert REJECT_PROMOTIONAL_POST in result.rejection_codes


@pytest.mark.parametrize(
    "text",
    [
        "We offer facials and peels. DM me to book!",
        "We provide medical weight loss. Contact us for pricing.",
        "I offer injector training, book now, limited time discount.",
    ],
)
def test_self_description_plus_any_cta_is_promotional(text):
    assert intent.detect_negative_signals(text).promotional != []


# ---------------------------------------------------------------------------
# Tests 4 and 5 -- marketing provider requests as adjacent digital growth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [SOLO_MD_MARKETING_AGENCY, NEW_MEDSPA_MARKETING_COMPANY],
)
def test_marketing_provider_requests_reach_the_ai(text):
    result = screen(text)

    assert intent.classify_intent(text).intent_type == (
        intent.INTENT_PROVIDER_REQUEST
    )
    assert result.passed is True
    assert REJECT_NO_SERVICE_MATCH not in result.rejection_codes


@pytest.mark.parametrize(
    "text",
    [SOLO_MD_MARKETING_AGENCY, NEW_MEDSPA_MARKETING_COMPANY],
)
def test_adjacent_matches_are_flagged_for_the_model_to_confirm(text):
    """
    Adjacency is a reason to *ask*, never a finding. The model rules on
    real fit from the post itself, and NO_SERVICE_MATCH still applies to its
    answer.
    """
    result = screen(text)
    services = intent.match_services(text)

    assert result.match_basis == "adjacent"
    assert services.needs_ai_confirmation is True
    assert services.strong_terms == []


def test_adjacent_growth_credits_only_services_brucetech_delivers():
    services = intent.match_services(SOLO_MD_MARKETING_AGENCY)

    assert set(services.categories) == {
        "website_development", "seo", "crm", "workflow_automation",
    }


def test_generic_marketing_talk_without_a_request_is_not_a_match():
    """A goal with no provider ask is not a lead."""
    musing = (
        "I own a med spa and I really need more patients this quarter, "
        "business has been slow since the summer started."
    )

    assert intent.adjacent_growth_evidence(musing) == []


def test_a_marketing_request_without_business_context_is_not_a_match():
    anonymous = (
        "Looking for a marketing agency that can bring in more clients, "
        "anyone got a recommendation?"
    )

    assert intent.match_services(anonymous).adjacent_growth == []


# ---------------------------------------------------------------------------
# Test 6 -- creative-only requests still fail
# ---------------------------------------------------------------------------

def test_influencer_only_request_has_no_brucetech_fit():
    result = screen(INFLUENCER_ONLY_REQUEST)

    assert intent.adjacent_growth_evidence(INFLUENCER_ONLY_REQUEST) == []
    assert result.service_matched is False
    assert result.passed is False


@pytest.mark.parametrize(
    "text",
    [
        "Med spa owner here, looking for a photographer to get me more "
        "clients on instagram, just content work.",
        "I own a clinic and need a PR firm to bring in new patients.",
        "Our practice is looking for a branding agency for a logo package "
        "to attract new patients.",
    ],
)
def test_creative_only_growth_requests_are_excluded(text):
    assert intent.adjacent_growth_evidence(text) == []


def test_creative_request_that_also_names_a_website_still_matches():
    """
    The exclusion is for requests that are *only* creative. A clinic asking
    for a photographer and a new website is still a website lead.
    """
    mixed = (
        "I own a med spa and need a photographer for content plus someone to "
        "rebuild our WordPress website to get more patients booking online."
    )

    assert screen(mixed).service_matched is True


# ---------------------------------------------------------------------------
# Tests 7 and 8 -- software comparison is tool research
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [MANGOMINT_OR_BOULEVARD, BOULEVARD_SWITCHING_RESEARCH],
)
def test_software_comparisons_are_tool_research(text):
    assert intent.classify_intent(text).intent_type == (
        intent.INTENT_TOOL_RESEARCH
    )


@pytest.mark.parametrize(
    "text",
    [MANGOMINT_OR_BOULEVARD, BOULEVARD_SWITCHING_RESEARCH],
)
def test_software_comparisons_never_produce_outreach(text):
    """
    Tool research may be recorded and reviewed. It is never auto-contacted:
    someone comparing products has not asked for an implementer.
    """
    assert intent.INTENT_TOOL_RESEARCH not in intent.OUTREACH_INTENTS
    assert screen(text).allows_outreach is False


def test_an_either_or_over_equipment_is_not_tool_research():
    """
    The software noun is what makes it tool research. "PicoWay or PicoSure
    laser" is equipment shopping.
    """
    equipment = (
        "PicoWay or PicoSure laser for a small clinic, and why? Trying to "
        "decide which one to buy before the end of the quarter."
    )

    result = screen(equipment)

    assert result.service_matched is False
    assert result.passed is False


# ---------------------------------------------------------------------------
# Tests 9, 10, 11 and the rest of the noise floor
# ---------------------------------------------------------------------------

def test_injector_hiring_is_rejected():
    result = screen(INJECTOR_HIRING)

    assert result.passed is False
    assert result.service_matched is False


def test_equipment_for_sale_is_rejected():
    result = screen(AESTHETIC_CHAIR_FOR_SALE)

    assert result.passed is False
    assert REJECT_PROMOTIONAL_POST in result.rejection_codes


def test_medical_device_shopping_is_rejected():
    result = screen(LASER_DEVICE_PURCHASE)

    assert result.passed is False
    assert result.service_matched is False


def test_device_shopping_suppresses_the_weak_service_path():
    """
    "Candela vs Cynosure platforms for a small clinic" contains "platform"
    and "clinic". Without the physical-goods guard that reads as a systems
    lead.
    """
    services = intent.match_services(LASER_DEVICE_PURCHASE)

    assert services.physical_goods is True
    assert services.matched is False


def test_it_hardware_is_not_treated_as_physical_goods():
    """
    The guard is for clinical equipment. A broken printer is BruceTech work
    and must not be swept up by it.
    """
    it_problem = (
        "Our office network keeps dropping and the printer is offline again. "
        "We need this fixed properly, we've been limping along for a month."
    )

    assert intent.is_shopping_for_physical_goods(it_problem) is False
    assert screen(it_problem).service_matched is True


@pytest.mark.parametrize(
    "name, text",
    sorted(MEDSPA_NOISE_FIXTURES.items()),
)
def test_every_noise_post_from_the_fresh_run_stops_before_the_ai(name, text):
    assert screen(text).passed is False, f"{name} would now reach the model"


@pytest.mark.parametrize(
    "name, text",
    sorted(MEDSPA_CANDIDATE_FIXTURES.items()),
)
def test_every_credible_post_from_the_fresh_run_reaches_the_ai(name, text):
    assert screen(text).passed is True, f"{name} still does not reach the model"


# ---------------------------------------------------------------------------
# The shape of a run, not a quota
# ---------------------------------------------------------------------------

def test_recall_and_precision_together_on_the_fresh_run_sample():
    """
    Every fresh-run post in one batch. This asserts the split is exactly the
    documented one, not a ratio: no quota is encoded anywhere in the
    pipeline, and none should be inferred from this test.
    """
    reaching = {
        name
        for name, text in {
            **MEDSPA_CANDIDATE_FIXTURES,
            **MEDSPA_NOISE_FIXTURES,
            "MANGOMINT_OR_BOULEVARD": MANGOMINT_OR_BOULEVARD,
            "BOULEVARD_SWITCHING_RESEARCH": BOULEVARD_SWITCHING_RESEARCH,
            "WEB_DESIGN_SELLER_CTA": WEB_DESIGN_SELLER_CTA,
        }.items()
        if screen(text).passed
    }

    assert reaching == set(MEDSPA_CANDIDATE_FIXTURES) | {
        "MANGOMINT_OR_BOULEVARD",
    }


def test_a_stale_medspa_lead_still_never_reaches_the_ai():
    """The recall work does not touch the age gate."""
    result = screen(MEDSPA_INVENTORY_NO_SYSTEMS, days_old=17)

    assert result.passed is False
    assert result.is_stale is True
    assert result.blocks_ai_call is True


def test_the_strongest_lead_shape_is_unaffected():
    """The pre-existing contract fixture still behaves identically."""
    result = screen(GOHIGHLEVEL_PROVIDER)

    assert result.passed is True
    assert result.match_basis == "named"


def test_the_match_basis_reaches_airtable_diagnostics():
    """
    The basis is only useful if an operator can see it. It rides in the
    AI Output blob for both the pre-AI rejection and the processed record.
    """
    import json

    import run_pipeline as rp

    update = rp.build_prefilter_rejection_update(
        "recAdjacent", screen(INFLUENCER_ONLY_REQUEST)
    )
    diagnostics = json.loads(update["fields"][rp.FIELD_AI_OUTPUT])

    assert diagnostics["match_basis"] == "none"

    blob = json.loads(
        rp.build_ai_output({}, rp.evaluate_lead({}), screen(SOLO_MD_MARKETING_AGENCY))
    )

    assert blob["match_basis"] == "adjacent"
