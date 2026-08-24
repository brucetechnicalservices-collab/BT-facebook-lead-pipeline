"""
Suggested Comment generation, em dash sanitisation, and TOOL_RESEARCH policy.

Two production changes, both narrow:

1. Outreach Ready leads now get a short public Facebook comment alongside the
   private DM. Both come from the same model response, both pass through the
   same sanitiser, and both obey exactly the same final outreach gate: if
   Outreach Ready is false, both Airtable fields are blank.

2. TOOL_RESEARCH can no longer become Qualified or Hot on score alone. A live
   Blue Collar record scored 76 and was written as Qualified while
   simultaneously being do_not_contact with a blank DM. Research is a human
   decision, so it is capped at Manual Review.

The em dash rule is enforced twice on purpose: the model is told never to use
one, and the sanitiser guarantees it. An instruction is a request; this is the
invariant.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import intent
import run_pipeline as rp
from normalization import contains_em_dash, sanitize_outreach_copy
from qualification import (
    REJECT_PROMOTIONAL_POST,
    TIER_MANUAL_REVIEW,
    TIER_REJECTED,
    evaluate_lead,
)

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

EM_DASH = "—"

STRONG_SIGNALS = {
    "intent_strength": "strong",
    "service_categories": ["crm", "workflow_automation"],
    "problem_specificity": "detailed",
    "business_context": "established_business",
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
}

SPECIFIC_DM = (
    "Hi, saw your post about the GoHighLevel setup for your HVAC team. We do "
    "exactly this kind of build at brucetech.ca. Worth a quick call this week?"
)

SPECIFIC_COMMENT = (
    "Sounds like you are past the research stage and need someone to actually "
    "get the setup working. I'll shoot you a quick DM with a couple thoughts."
)


def decide(**kwargs):
    params = {
        "suggested_dm": SPECIFIC_DM,
        "suggested_comment": SPECIFIC_COMMENT,
        "recommended_channel": "direct_message",
        "now": NOW,
        **kwargs,
    }
    return evaluate_lead(STRONG_SIGNALS, **params)


# ---------------------------------------------------------------------------
# Tests 1 and 2 -- Suggested Comment is populated for real outreach
# ---------------------------------------------------------------------------

def test_an_outreach_ready_provider_request_gets_a_comment():
    decision = decide(intent_type=intent.INTENT_PROVIDER_REQUEST)

    assert decision.outreach_ready is True
    assert decision.suggested_comment != ""


def test_an_outreach_ready_business_pain_lead_gets_a_comment():
    decision = decide(intent_type=intent.INTENT_BUSINESS_PAIN)

    assert decision.outreach_ready is True
    assert decision.suggested_comment != ""


# ---------------------------------------------------------------------------
# Tests 3, 4, 5 -- what the comment must and must not contain
# ---------------------------------------------------------------------------

def test_the_comment_offers_to_move_the_conversation_to_a_dm():
    """
    The invariant is that the comment mentions a DM, not any exact phrase.
    The instructions ask the model to vary the closing across leads.
    """
    decision = decide(intent_type=intent.INTENT_PROVIDER_REQUEST)
    lowered = decision.suggested_comment.lower()

    assert "dm" in lowered or "message you" in lowered


@pytest.mark.parametrize(
    "forbidden",
    ["http://", "https://", "brucetech.ca", "www.", ".com/"],
)
def test_the_comment_carries_no_links(forbidden):
    decision = decide(intent_type=intent.INTENT_PROVIDER_REQUEST)

    assert forbidden not in decision.suggested_comment.lower()


def test_the_comment_is_short_enough_to_read_as_a_comment():
    """
    The target is 12 to 35 words. The bound here is deliberately loose so the
    test pins "short public comment" rather than a word count the model
    cannot hit exactly.
    """
    decision = decide(intent_type=intent.INTENT_PROVIDER_REQUEST)
    words = decision.suggested_comment.split()

    assert 8 <= len(words) <= 60
    assert len(decision.suggested_comment) <= 400


def test_the_dm_may_still_mention_brucetech():
    """The private message keeps the pitch; only the public comment cannot."""
    decision = decide(intent_type=intent.INTENT_PROVIDER_REQUEST)

    assert "brucetech.ca" in decision.suggested_dm


# ---------------------------------------------------------------------------
# Tests 6, 7, 8 -- the gate
# ---------------------------------------------------------------------------

def test_tool_research_gets_no_comment():
    decision = decide(intent_type=intent.INTENT_TOOL_RESEARCH)

    assert decision.suggested_comment == ""
    assert decision.suggested_dm == ""


def test_a_rejected_lead_gets_no_comment():
    decision = evaluate_lead(
        {**STRONG_SIGNALS, "promotional_post": True},
        suggested_dm=SPECIFIC_DM,
        suggested_comment=SPECIFIC_COMMENT,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_PROVIDER_REQUEST,
    )

    assert decision.tier == TIER_REJECTED
    assert decision.suggested_comment == ""
    assert decision.suggested_dm == ""


def test_a_model_comment_is_discarded_when_outreach_is_not_ready():
    """
    The model wrote copy. The gate says no. Airtable gets nothing.
    """
    decision = decide(
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        recommended_channel="do_not_contact",
    )

    assert decision.outreach_ready is False
    assert decision.suggested_comment == ""
    assert decision.suggested_dm == ""


@pytest.mark.parametrize(
    "kwargs",
    [
        {"intent_type": intent.INTENT_TOOL_RESEARCH},
        {"intent_type": intent.INTENT_UNRELATED},
        {"recommended_channel": "do_not_contact"},
        {"suggested_dm": ""},
    ],
)
def test_outreach_not_ready_always_blanks_both_fields(kwargs):
    decision = decide(**kwargs)

    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""
    assert decision.suggested_comment == ""


def test_the_airtable_payload_blanks_both_fields_together():
    decision = decide(intent_type=intent.INTENT_TOOL_RESEARCH)
    payload = rp.map_decision_to_airtable(decision, STRONG_SIGNALS)

    assert payload[rp.FIELD_SUGGESTED_DM] == ""
    assert payload[rp.FIELD_SUGGESTED_COMMENT] == ""
    assert payload[rp.FIELD_OUTREACH_READY] is False


def test_a_prefiltered_record_blanks_the_comment_too():
    from qualification import prefilter_post

    prefilter = prefilter_post("Just saying hello to everyone in the group.")
    update = rp.build_prefilter_rejection_update("recX", prefilter)

    assert update["fields"][rp.FIELD_SUGGESTED_COMMENT] == ""
    assert update["fields"][rp.FIELD_SUGGESTED_DM] == ""


# ---------------------------------------------------------------------------
# Test 9 -- existing DM behaviour is unchanged
# ---------------------------------------------------------------------------

def test_the_dm_is_unchanged_for_a_qualified_outreach_ready_lead():
    decision = decide(intent_type=intent.INTENT_PROVIDER_REQUEST)

    assert decision.qualified is True
    assert decision.outreach_ready is True
    assert decision.suggested_dm == SPECIFIC_DM
    assert decision.recommended_channel == "direct_message"


# ---------------------------------------------------------------------------
# Tests 10, 11, 12 -- Airtable and the single model call
# ---------------------------------------------------------------------------

def test_the_schema_preflight_requires_the_suggested_comment_field():
    assert rp.FIELD_SUGGESTED_COMMENT in rp.REQUIRED_RAW_SIGNAL_FIELDS
    assert rp.KNOWN_FIELD_IDS[rp.FIELD_SUGGESTED_COMMENT] == "fldFYDjbtu6gLPT1B"


def test_the_preflight_accepts_the_field_by_id_when_it_was_renamed(
    monkeypatch, capsys
):
    """A renamed column is still the same field, and the ID proves it."""
    fields = [
        {"name": name, "id": f"fld{index:013d}"}
        for index, name in enumerate(rp.REQUIRED_RAW_SIGNAL_FIELDS)
        if name != rp.FIELD_SUGGESTED_COMMENT
    ]
    fields.append({"name": "Public Comment", "id": "fldFYDjbtu6gLPT1B"})

    class FakeResponse:
        def json(self):
            return {"tables": [{"name": "Raw Signals", "fields": fields}]}

    monkeypatch.setenv("AIRTABLE_BASE_ID", "appTest")
    monkeypatch.setenv("AIRTABLE_TABLE_NAME", "Raw Signals")
    monkeypatch.setattr(
        rp, "request_with_retry", lambda *a, **k: FakeResponse()
    )
    monkeypatch.setattr(rp, "airtable_headers", lambda: {})

    rp.validate_airtable_schema()

    assert "validated" in capsys.readouterr().out


def test_the_comment_is_not_a_protected_human_field():
    """AI-owned output, exactly like Suggested DM."""
    assert rp.FIELD_SUGGESTED_COMMENT not in rp.READ_ONLY_FIELDS

    kept = rp.strip_read_only_fields(
        {rp.FIELD_SUGGESTED_COMMENT: "text", rp.FIELD_HUMAN_DECISION: "Approve"}
    )

    assert rp.FIELD_SUGGESTED_COMMENT in kept
    assert rp.FIELD_HUMAN_DECISION not in kept


def test_the_comment_comes_from_the_same_model_response(monkeypatch):
    """
    Test 11 and 12 together: one call, both fields. A second request for the
    comment would show up as a second extraction here.
    """
    calls = []

    def fake_extract(fields):
        calls.append(fields)
        return {
            **STRONG_SIGNALS,
            "suggested_dm": SPECIFIC_DM,
            "suggested_comment": SPECIFIC_COMMENT,
            "lead_summary": "x",
            "evidence": "y",
            "service_match": "CRM",
            "recommended_channel": "direct_message",
        }

    monkeypatch.setattr(rp, "extract_signals", fake_extract)

    decision, signals = rp.qualify_post(
        {rp.FIELD_TEXT: "Need the whole GoHighLevel setup for our HVAC team."},
        prefilter=None,
        now=NOW,
    )

    assert len(calls) == 1
    assert decision.suggested_dm != ""
    assert decision.suggested_comment != ""
    assert signals["suggested_comment"] == SPECIFIC_COMMENT


def test_the_model_schema_asks_for_both_fields_in_one_response():
    properties = rp.LEAD_SCHEMA["properties"]

    assert "suggested_dm" in properties
    assert "suggested_comment" in properties
    assert "suggested_comment" in rp.LEAD_SCHEMA["required"]


def test_the_instructions_describe_the_public_comment():
    instructions = rp.SYSTEM_INSTRUCTIONS

    assert "suggested_comment" in instructions
    assert "PUBLIC" in instructions
    assert "EM DASH" in instructions


# ---------------------------------------------------------------------------
# Tests 13 to 18 -- no em dashes, ever
# ---------------------------------------------------------------------------

def test_a_model_dm_containing_an_em_dash_is_normalised():
    decision = decide(
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        suggested_dm=f"Hi Matt {EM_DASH} I saw your post about lead generation.",
    )

    assert EM_DASH not in decision.suggested_dm
    assert decision.suggested_dm == (
        "Hi Matt, I saw your post about lead generation."
    )


def test_a_model_comment_containing_an_em_dash_is_normalised():
    decision = decide(
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        suggested_comment=(
            f"That workflow could work well {EM_DASH} I'll send you a quick DM."
        ),
    )

    assert EM_DASH not in decision.suggested_comment
    assert decision.suggested_comment == (
        "That workflow could work well, I'll send you a quick DM."
    )


def test_a_generated_outreach_ready_dm_contains_no_em_dash():
    decision = decide(
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        suggested_dm=f"Hi {EM_DASH} we build CRMs {EM_DASH} at brucetech.ca. Chat?",
    )

    assert decision.outreach_ready is True
    assert (EM_DASH in decision.suggested_dm) is False


def test_a_generated_outreach_ready_comment_contains_no_em_dash():
    decision = decide(
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        suggested_comment=(
            f"Nice setup {EM_DASH} I'll send a DM with a couple ideas."
        ),
    )

    assert decision.outreach_ready is True
    assert (EM_DASH in decision.suggested_comment) is False


def test_the_airtable_payload_never_carries_an_em_dash():
    decision = decide(
        intent_type=intent.INTENT_PROVIDER_REQUEST,
        suggested_dm=f"Hi Matt {EM_DASH} saw your post. brucetech.ca?",
        suggested_comment=f"Good problem {EM_DASH} I'll DM you a few thoughts.",
    )
    payload = rp.map_decision_to_airtable(decision, STRONG_SIGNALS)

    assert EM_DASH not in payload[rp.FIELD_SUGGESTED_DM]
    assert EM_DASH not in payload[rp.FIELD_SUGGESTED_COMMENT]


def test_the_sanitiser_does_not_damage_valid_copy():
    for text in (SPECIFIC_DM, SPECIFIC_COMMENT):
        assert sanitize_outreach_copy(text) == text


@pytest.mark.parametrize(
    "raw, expected",
    [
        (
            f"Hi Matt {EM_DASH} I saw your post about lead generation.",
            "Hi Matt, I saw your post about lead generation.",
        ),
        (
            f"That setup can work well {EM_DASH} especially with a CRM.",
            "That setup can work well, especially with a CRM.",
        ),
        (
            f"the intake flow{EM_DASH}the part that matters{EM_DASH}is broken",
            "the intake flow, the part that matters, is broken",
        ),
    ],
)
def test_normalised_copy_stays_readable(raw, expected):
    result = sanitize_outreach_copy(raw)

    assert result == expected
    assert "  " not in result
    assert " ," not in result
    assert contains_em_dash(result) is False


def test_the_sanitiser_leaves_urls_and_hyphens_alone():
    text = "See brucetech.ca/managed-it for the well-structured plan."

    assert sanitize_outreach_copy(text) == text


def test_a_tight_range_is_not_a_sentence_break():
    """An en dash between numbers is a range, not a dash to normalise."""
    text = "We are open Mon–Fri, 9–5."

    assert sanitize_outreach_copy(text) == text


def test_the_sanitiser_keeps_a_blank_blank():
    assert sanitize_outreach_copy("") == ""
    assert sanitize_outreach_copy(None) == ""
    assert sanitize_outreach_copy("   ") == ""


# ---------------------------------------------------------------------------
# Tests 19 to 24 -- TOOL_RESEARCH policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("confidence", [0.9, 0.6])
def test_high_scoring_tool_research_is_manual_review(confidence):
    """Test 19: score 90 or thereabouts must not qualify."""
    decision = evaluate_lead(
        {**STRONG_SIGNALS, "classification_confidence": confidence},
        suggested_dm=SPECIFIC_DM,
        suggested_comment=SPECIFIC_COMMENT,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_TOOL_RESEARCH,
    )

    assert decision.lead_score >= 80
    assert decision.tier == TIER_MANUAL_REVIEW
    assert decision.qualified is False
    assert decision.manual_review is True
    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""
    assert decision.suggested_comment == ""
    assert decision.recommended_channel == "do_not_contact"


def test_mid_scoring_tool_research_is_also_manual_review():
    """Test 20: a score just over the qualification threshold."""
    decision = evaluate_lead(
        {
            **STRONG_SIGNALS,
            "intent_strength": "moderate",
            "urgency": "low",
            "business_impact": "low",
            "purchase_signal": "researching",
        },
        suggested_dm=SPECIFIC_DM,
        suggested_comment=SPECIFIC_COMMENT,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_TOOL_RESEARCH,
    )

    assert 55 <= decision.lead_score
    assert decision.tier == TIER_MANUAL_REVIEW
    assert decision.qualified is False
    assert decision.manual_review is True
    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""
    assert decision.suggested_comment == ""
    assert decision.recommended_channel == "do_not_contact"


def test_a_hard_rejection_still_beats_tool_research_manual_review():
    """Test 21: hard rejections were never weakened."""
    decision = evaluate_lead(
        {**STRONG_SIGNALS, "promotional_post": True},
        suggested_dm=SPECIFIC_DM,
        suggested_comment=SPECIFIC_COMMENT,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=intent.INTENT_TOOL_RESEARCH,
    )

    assert REJECT_PROMOTIONAL_POST in decision.hard_rejection_codes
    assert decision.tier == TIER_REJECTED
    assert decision.qualified is False
    assert decision.manual_review is False
    assert decision.outreach_ready is False
    assert decision.suggested_dm == ""
    assert decision.suggested_comment == ""


def test_a_stale_tool_research_record_is_rejected_not_manual_review():
    decision = evaluate_lead(
        STRONG_SIGNALS,
        suggested_dm=SPECIFIC_DM,
        suggested_comment=SPECIFIC_COMMENT,
        recommended_channel="direct_message",
        post_time=(NOW - timedelta(days=30)).isoformat(),
        now=NOW,
        max_post_age_days=5,
        intent_type=intent.INTENT_TOOL_RESEARCH,
    )

    assert decision.tier == TIER_REJECTED
    assert decision.manual_review is False


def test_provider_request_behaviour_is_unchanged():
    """Test 22."""
    decision = decide(intent_type=intent.INTENT_PROVIDER_REQUEST)

    assert decision.lead_score >= 80
    assert decision.qualified is True
    assert decision.tier in {"Hot", "Qualified"}
    assert decision.outreach_ready is True
    assert decision.suggested_dm == SPECIFIC_DM


def test_business_pain_behaviour_is_unchanged():
    """Test 23."""
    decision = decide(intent_type=intent.INTENT_BUSINESS_PAIN)

    assert decision.lead_score >= 70
    assert decision.qualified is True
    assert decision.outreach_ready is True
    assert decision.suggested_dm == SPECIFIC_DM


def test_implementation_request_behaviour_is_unchanged():
    decision = decide(intent_type=intent.INTENT_IMPLEMENTATION_REQUEST)

    assert decision.qualified is True
    assert decision.outreach_ready is True


def test_the_pool_contractor_fixture_end_to_end():
    """
    Test 24: the exact live record, from post text to Airtable payload.
    """
    from qualification import prefilter_post
    from tests.fixtures import POOL_BUILDER_SOFTWARE_STACK

    prefilter = prefilter_post(
        POOL_BUILDER_SOFTWARE_STACK,
        post_time=(NOW - timedelta(days=1)).isoformat(),
        now=NOW,
        max_post_age_days=5,
    )

    assert prefilter.intent_type == intent.INTENT_TOOL_RESEARCH
    assert prefilter.passed is True

    decision = evaluate_lead(
        STRONG_SIGNALS,
        suggested_dm=SPECIFIC_DM,
        suggested_comment=SPECIFIC_COMMENT,
        recommended_channel="direct_message",
        now=NOW,
        intent_type=prefilter.intent_type,
        post_text=POOL_BUILDER_SOFTWARE_STACK,
    )
    payload = rp.map_decision_to_airtable(
        decision, STRONG_SIGNALS, prefilter=prefilter
    )

    assert payload[rp.FIELD_QUALIFIED] is False
    assert payload[rp.FIELD_MANUAL_REVIEW] is True
    assert payload[rp.FIELD_LEAD_TIER] == TIER_MANUAL_REVIEW
    assert payload[rp.FIELD_OUTREACH_READY] is False
    assert payload[rp.FIELD_SUGGESTED_DM] == ""
    assert payload[rp.FIELD_SUGGESTED_COMMENT] == ""
    assert payload[rp.FIELD_RECOMMENDED_CHANNEL] == "do_not_contact"


def test_the_model_analysis_survives_for_diagnostics():
    """
    The gate blanks the sales fields. It does not destroy the model's work:
    AI Output keeps the score, the signals, and the copy it wrote.
    """
    decision = decide(intent_type=intent.INTENT_TOOL_RESEARCH)
    payload = rp.map_decision_to_airtable(decision, STRONG_SIGNALS)
    blob = json.loads(payload[rp.FIELD_AI_OUTPUT])

    assert blob["lead_score"] >= 80
    assert blob["tier"] == TIER_MANUAL_REVIEW
    assert blob["outreach_copy"]["written"] is False
    assert blob["signals"]["service_categories"] == ["crm", "workflow_automation"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        (EM_DASH, ""),
        (f"{EM_DASH}start", "start"),
        (f"end{EM_DASH}", "end"),
        (f"{EM_DASH} {EM_DASH} {EM_DASH}", ""),
        (f"a,{EM_DASH}b", "a, b"),
        (f"Hi.{EM_DASH}Thanks", "Hi. Thanks"),
    ],
)
def test_degenerate_dash_input_never_leaves_a_dangling_comma(raw, expected):
    """
    Found by fuzzing the sanitiser before shipping. A dash opening or closing
    the copy used to leave a leading comma, which is worse than the dash was.
    """
    result = sanitize_outreach_copy(raw)

    assert result == expected
    assert not result.startswith(",")
    assert not result.endswith(",")
    assert ",," not in result


def test_the_gate_and_the_sanitiser_hold_across_every_combination():
    """
    Every intent, channel, and copy state in one sweep. Two invariants:
    nothing is written without Outreach Ready, and nothing written carries an
    em dash.
    """
    intents = [
        intent.INTENT_PROVIDER_REQUEST,
        intent.INTENT_IMPLEMENTATION_REQUEST,
        intent.INTENT_BUSINESS_PAIN,
        intent.INTENT_TOOL_RESEARCH,
        intent.INTENT_GENERAL_ADVICE,
        intent.INTENT_UNRELATED,
        None,
    ]

    for intent_type in intents:
        for channel in ("direct_message", "do_not_contact"):
            for dm in (f"Hi {EM_DASH} we can help at brucetech.ca. Chat?", ""):
                decision = evaluate_lead(
                    STRONG_SIGNALS,
                    suggested_dm=dm,
                    suggested_comment=f"Nice {EM_DASH} I'll DM you.",
                    recommended_channel=channel,
                    now=NOW,
                    intent_type=intent_type,
                )
                payload = rp.map_decision_to_airtable(decision, STRONG_SIGNALS)

                written = (
                    payload[rp.FIELD_SUGGESTED_DM],
                    payload[rp.FIELD_SUGGESTED_COMMENT],
                )

                if not decision.outreach_ready:
                    assert written == ("", ""), (
                        f"{intent_type}/{channel} wrote copy without "
                        f"Outreach Ready"
                    )

                for value in written:
                    assert EM_DASH not in value

                if (
                    intent_type == intent.INTENT_TOOL_RESEARCH
                    and not decision.hard_rejected
                ):
                    assert payload[rp.FIELD_QUALIFIED] is False
