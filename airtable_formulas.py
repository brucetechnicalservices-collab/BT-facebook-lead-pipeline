"""
Airtable formula sources for the operator-visible intent columns.

WHAT THIS IS NOT
================

These formulas are **not authoritative**. ``intent.py`` decides what the
pipeline does; nothing here can qualify a lead, reach the model, or override a
rejection. ``Prequalification`` in particular is a machine hint the pipeline
grants no power at all -- see run_pipeline.is_airtable_prequalified.

WHAT THIS IS FOR
================

An operator filtering the Raw Signals table by ``Intent Type`` is reading
Airtable's formula, not Python's classification. When the two disagree the
table lies to the person reading it. In the 2026-08-19 fresh run they
disagreed visibly:

* "I'm a solo MD looking for marketing agency to get me patients" --
  Python said PROVIDER_REQUEST, Airtable said Other, Provider Intent
  Signal = 0.
* "Mangomint or boulevard POS system and why?" -- Python (after the recall
  fix) says TOOL_RESEARCH, Airtable said Other.

The formulas below close those obvious gaps. They are a deliberately coarse
mirror of the Python patterns, not a reimplementation: Airtable formulas
cannot express the commercial-context gating, the physical-goods guard, or
the adjacency rules, and they are not asked to.

HOW TO APPLY
============

Print the formula and paste it into the field's configuration::

    python -c "import airtable_formulas as f; print(f.INTENT_TYPE)"

Nothing in this repository writes to the base schema. Applying these is a
manual step, and the pipeline behaves identically whether or not you do.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Shared regex fragments
#
# Written in the subset both Airtable's REGEX_MATCH and Python's re accept, so
# tests/test_airtable_formulas.py can run the exact strings the operator
# pastes and prove they agree with the classifier on the cases below.
# ---------------------------------------------------------------------------

#: Someone asking for a person or company to hire, including the marketing
#: and agency phrasings the previous formula missed entirely.
PROVIDER_INTENT_REGEX = (
    r"(look(ing)? for|looking to hire|need|want|seeking|in search of|"
    r"recommendations? for|any(one|body) (know|recommend|suggest))"
    r"[^.!?]{0,40}"
    r"(someone|somebody|person|company|companies|agency|agencies|firm|"
    r"freelancer|contractor|developer|designer|consultant|expert|"
    r"specialist|professional|provider|vendor|technician|team|pro)"
)

#: Marketing-shaped provider asks, which read as a provider request to a
#: human scanning the table even though the noun is a marketing one.
PROVIDER_MARKETING_REGEX = (
    r"(marketing|ads?|advertising|seo|lead gen(eration)?) "
    r"(agency|agencies|company|companies|firm|team|partner|platform|person)"
)

#: Comparing or switching products, rather than asking who should build it.
RESEARCH_INTENT_REGEX = (
    r"(pros and cons|which (crm|software|system|platform|app|pos)|"
    r"what (crm|software|system|platform|app|pos)|"
    r"(thinking|considering|looking) (about |of |at )?"
    r"(switching|moving|migrating|changing)( over)? to|"
    r"switch(ing)? (from|off)|"
    r"[a-z0-9.']{3,} (or|vs\.?|versus) [a-z0-9.']{3,}"
    r"( \w+){0,2} (pos|crm|software|system|platform|app|point of sale))"
)


def _airtable(field: str, pattern: str) -> str:
    """Wrap a pattern as a case-insensitive Airtable REGEX_MATCH call."""
    escaped = pattern.replace('"', '\\"')
    return f'REGEX_MATCH(LOWER({{{field}}}), "{escaped}")'


PROVIDER_INTENT_SIGNAL = "IF(OR({provider}, {marketing}), 1, 0)".format(
    provider=_airtable("Text", PROVIDER_INTENT_REGEX),
    marketing=_airtable("Text", PROVIDER_MARKETING_REGEX),
)

RESEARCH_INTENT_SIGNAL = "IF({research}, 1, 0)".format(
    research=_airtable("Text", RESEARCH_INTENT_REGEX),
)

#: Provider outranks research, matching classify_intent's precedence: a
#: request to hire someone that happens to name two products is a hire.
INTENT_TYPE = (
    'IF({Provider Intent Signal} = 1, "Provider Request", '
    'IF({Research Intent Signal} = 1, "Research", '
    'IF({Service Signal} = 1, "Possible Service Need", "Other")))'
)

FORMULAS = {
    "Provider Intent Signal": PROVIDER_INTENT_SIGNAL,
    "Research Intent Signal": RESEARCH_INTENT_SIGNAL,
    "Intent Type": INTENT_TYPE,
}

# --- Python mirrors, for tests only ----------------------------------------

_PROVIDER_RE = re.compile(PROVIDER_INTENT_REGEX, re.IGNORECASE)
_PROVIDER_MARKETING_RE = re.compile(PROVIDER_MARKETING_REGEX, re.IGNORECASE)
_RESEARCH_RE = re.compile(RESEARCH_INTENT_REGEX, re.IGNORECASE)


def provider_intent_signal(text: str) -> int:
    """Evaluate the Provider Intent Signal formula in Python."""
    body = str(text or "").lower()
    return int(
        bool(_PROVIDER_RE.search(body) or _PROVIDER_MARKETING_RE.search(body))
    )


def research_intent_signal(text: str) -> int:
    """Evaluate the Research Intent Signal formula in Python."""
    return int(bool(_RESEARCH_RE.search(str(text or "").lower())))


def intent_type(text: str, *, service_signal: int = 0) -> str:
    """Evaluate the Intent Type formula in Python."""
    if provider_intent_signal(text):
        return "Provider Request"
    if research_intent_signal(text):
        return "Research"
    if service_signal:
        return "Possible Service Need"
    return "Other"
