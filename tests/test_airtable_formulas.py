"""
Consistency tests for the operator-visible Airtable intent formulas.

These formulas decide nothing. They exist so a human filtering the Raw
Signals table sees roughly what Python decided, and the 2026-08-19 fresh run
showed them visibly disagreeing: a marketing-agency provider request read as
"Other" with Provider Intent Signal = 0.

What is asserted here is that the exact regex strings an operator pastes into
Airtable agree with the classifier on the obvious cases. Where the formula is
deliberately coarser than Python, that is asserted too, so the gap is a
recorded decision rather than a surprise.
"""

from __future__ import annotations

import pytest

import airtable_formulas as formulas
import intent
from tests.fixtures import (
    BOULEVARD_SWITCHING_RESEARCH,
    GOHIGHLEVEL_PROVIDER,
    LASER_DEVICE_PURCHASE,
    MANGOMINT_OR_BOULEVARD,
    NEW_MEDSPA_MARKETING_COMPANY,
    SKINCARE_INGREDIENT_CHAT,
    SOLO_MD_MARKETING_AGENCY,
)


# ---------------------------------------------------------------------------
# The four cases named in the fresh-run review
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        SOLO_MD_MARKETING_AGENCY,
        NEW_MEDSPA_MARKETING_COMPANY,
        "looking for a marketing agency",
        "looking for marketing company",
    ],
)
def test_marketing_provider_requests_now_set_provider_intent_signal(text):
    assert formulas.provider_intent_signal(text) == 1
    assert formulas.intent_type(text) == "Provider Request"


@pytest.mark.parametrize(
    "text",
    [
        MANGOMINT_OR_BOULEVARD,
        BOULEVARD_SWITCHING_RESEARCH,
        "thinking of switching to Mangomint",
        "Mangomint or Boulevard POS",
    ],
)
def test_software_comparisons_now_set_research_intent_signal(text):
    assert formulas.research_intent_signal(text) == 1
    assert formulas.intent_type(text) == "Research"


# ---------------------------------------------------------------------------
# Agreement with the authoritative classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        GOHIGHLEVEL_PROVIDER,
        SOLO_MD_MARKETING_AGENCY,
        NEW_MEDSPA_MARKETING_COMPANY,
    ],
)
def test_the_formula_agrees_with_python_on_provider_requests(text):
    assert intent.classify_intent(text).intent_type == (
        intent.INTENT_PROVIDER_REQUEST
    )
    assert formulas.intent_type(text) == "Provider Request"


@pytest.mark.parametrize(
    "text",
    [MANGOMINT_OR_BOULEVARD, BOULEVARD_SWITCHING_RESEARCH],
)
def test_the_formula_agrees_with_python_on_tool_research(text):
    assert intent.classify_intent(text).intent_type == (
        intent.INTENT_TOOL_RESEARCH
    )
    assert formulas.intent_type(text) == "Research"


def test_ordinary_chatter_stays_other():
    assert formulas.intent_type(SKINCARE_INGREDIENT_CHAT) == "Other"


# ---------------------------------------------------------------------------
# Known coarseness, asserted so it stays a decision
# ---------------------------------------------------------------------------

def test_the_formula_is_coarser_than_python_and_that_is_harmless():
    """
    "Candela vs Cynosure platforms" trips the formula's either/or pattern.
    Python rejects the post outright via the physical-goods guard, which is
    the only opinion that governs spend. An Airtable formula cannot express
    that guard, and does not need to: it labels a row, it does not gate a
    call.
    """
    assert formulas.research_intent_signal(LASER_DEVICE_PURCHASE) == 1
    assert intent.classify_intent(LASER_DEVICE_PURCHASE).intent_type == (
        intent.INTENT_UNRELATED
    )
    assert intent.match_services(LASER_DEVICE_PURCHASE).matched is False


# ---------------------------------------------------------------------------
# The pasted artefact
# ---------------------------------------------------------------------------

def test_every_formula_is_a_complete_airtable_expression():
    for name, formula in formulas.FORMULAS.items():
        assert formula.count("(") == formula.count(")"), name
        assert formula.startswith("IF("), name


def test_the_regexes_in_the_formulas_are_the_ones_under_test():
    """No drift between what is tested and what an operator pastes."""
    assert formulas.PROVIDER_INTENT_REGEX in formulas.PROVIDER_INTENT_SIGNAL
    assert formulas.PROVIDER_MARKETING_REGEX in formulas.PROVIDER_INTENT_SIGNAL
    assert formulas.RESEARCH_INTENT_REGEX in formulas.RESEARCH_INTENT_SIGNAL


def test_the_formulas_are_not_referenced_by_the_pipeline():
    """
    Operator visibility only. If run_pipeline ever imports this module, the
    formulas have started deciding something.
    """
    source = open("run_pipeline.py", encoding="utf-8").read()

    assert "airtable_formulas" not in source
