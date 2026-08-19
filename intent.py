"""
Deterministic intent classification and BruceTech service matching.

This module answers two questions about a raw Facebook post, before any AI
call is made:

1.  **What is the author actually asking for?** (``classify_intent``)
2.  **Does that map onto something BruceTech sells?** (``match_services``)

Both answers are computed from word-boundary-safe regular expressions, so the
result is reproducible, cheap, and unit-testable without credentials.

Why this module exists
----------------------

The previous prefilter matched service keywords with a plain substring test::

    "api" in "working capital"   # -> True

That single behaviour let an excavation-financing post register as an API
integration lead. Every term here is compiled into ``\\b``-anchored regex, so
``api`` matches "integrating an API" and never "working capital".

The Airtable ``Service Signal`` formula was fixed the same way. This module is
the Python half of that protection: the pipeline must not depend on Airtable
formula timing to stay safe.

The other failure mode this module addresses is *solution hopping* — reading
"business owner has a problem" as "BruceTech sales opportunity". Intent is
classified independently of service match, and the prefilter requires **both**.
"I need a virtual assistant" is a genuine PROVIDER_REQUEST, but it matches no
BruceTech service, so it is rejected.

Like ``qualification`` and ``normalization``, this module has no environment,
network, or third-party dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Pattern

# ---------------------------------------------------------------------------
# Intent types
# ---------------------------------------------------------------------------

INTENT_PROVIDER_REQUEST = "PROVIDER_REQUEST"
INTENT_IMPLEMENTATION_REQUEST = "IMPLEMENTATION_REQUEST"
INTENT_BUSINESS_PAIN = "BUSINESS_PAIN"
INTENT_TOOL_RESEARCH = "TOOL_RESEARCH"
INTENT_GENERAL_ADVICE = "GENERAL_ADVICE"
INTENT_UNRELATED = "UNRELATED"

#: Every intent type, in descending order of commercial value.
INTENT_TYPES = (
    INTENT_PROVIDER_REQUEST,
    INTENT_IMPLEMENTATION_REQUEST,
    INTENT_BUSINESS_PAIN,
    INTENT_TOOL_RESEARCH,
    INTENT_GENERAL_ADVICE,
    INTENT_UNRELATED,
)

#: Human-readable labels, matching the Airtable ``Intent Type`` formula output.
INTENT_LABELS: dict[str, str] = {
    INTENT_PROVIDER_REQUEST: "Provider Request",
    INTENT_IMPLEMENTATION_REQUEST: "Implementation Request",
    INTENT_BUSINESS_PAIN: "Business Pain",
    INTENT_TOOL_RESEARCH: "Tool Research",
    INTENT_GENERAL_ADVICE: "General Advice",
    INTENT_UNRELATED: "Unrelated",
}

#: Intents that may reach the AI at all. GENERAL_ADVICE and UNRELATED never do.
AI_ELIGIBLE_INTENTS = frozenset(
    {
        INTENT_PROVIDER_REQUEST,
        INTENT_IMPLEMENTATION_REQUEST,
        INTENT_BUSINESS_PAIN,
        INTENT_TOOL_RESEARCH,
    }
)

#: Intents that may ever produce automatic outreach.
#:
#: TOOL_RESEARCH is deliberately absent: someone asking "what CRM does
#: everyone use?" is doing research, not shopping for an implementer. Those
#: records land in Manual Review so a human decides.
OUTREACH_INTENTS = frozenset(
    {
        INTENT_PROVIDER_REQUEST,
        INTENT_IMPLEMENTATION_REQUEST,
        INTENT_BUSINESS_PAIN,
    }
)


# ---------------------------------------------------------------------------
# Word-boundary-safe term matching
# ---------------------------------------------------------------------------

def compile_term(term: str) -> Pattern[str]:
    """
    Compile one keyword into a word-boundary-anchored pattern.

    * Internal whitespace becomes ``\\s+`` so "microsoft 365" also matches
      "Microsoft  365" and a line break between the words.
    * A trailing optional ``s`` lets "backup" match "backups" without letting
      "api" match "capital".
    * ``\\b`` on both ends is what fixes the substring false positives.
    """
    escaped = re.escape(term.strip().lower())

    # re.escape() escapes spaces on some Python versions and not others, so
    # collapse either form into a flexible whitespace matcher.
    escaped = re.sub(r"(?:\\?\s)+", r"\\s+", escaped)

    return re.compile(rf"\b{escaped}(?:s)?\b", re.IGNORECASE)


def _compile_all(terms: Iterable[str]) -> tuple[tuple[str, Pattern[str]], ...]:
    return tuple((term, compile_term(term)) for term in terms)


def _hits(
    text: str,
    compiled: tuple[tuple[str, Pattern[str]], ...],
) -> list[str]:
    """Return every term whose pattern occurs in ``text``, order preserved."""
    return [term for term, pattern in compiled if pattern.search(text)]


def matches_any(text: Any, terms: Iterable[str]) -> bool:
    """Word-boundary-safe membership test, exposed for tests and callers."""
    body = str(text or "")
    return any(compile_term(term).search(body) for term in terms)


# ---------------------------------------------------------------------------
# BruceTech service vocabulary
#
# STRONG terms are unambiguous: one hit establishes a service match.
#
# WEAK terms are real BruceTech vocabulary that also occurs in unrelated
# business conversation ("payment plan", "security deposit", "networking
# event"). A weak term alone never establishes a match — see
# ``match_services`` for the exact rule.
#
# Categories align with qualification.SERVICE_CATEGORIES so the deterministic
# prefilter and the AI schema speak the same language.
# ---------------------------------------------------------------------------

STRONG_SERVICE_TERMS: dict[str, tuple[str, ...]] = {
    "website_development": (
        "website", "web site", "webpage", "landing page", "web developer",
        "web development", "web design", "web designer", "wordpress",
        "squarespace", "wix", "webflow", "website redesign", "site redesign",
        "contact form", "booking system", "booking page", "online booking",
        "appointment booking",
    ),
    "ecommerce": (
        "ecommerce", "e-commerce", "online store", "online shop", "shopify",
        "woocommerce", "shopping cart", "checkout page", "stripe",
        "payment gateway", "payment processor", "online payment",
        "payment integration",
    ),
    "seo": (
        "seo", "search engine optimization", "search engine optimisation",
        "google business profile", "google my business", "local search ranking",
    ),
    "website_maintenance": (
        "website maintenance", "site maintenance", "web hosting", "hosting",
        "domain name", "dns", "ssl certificate", "website down",
        "site is down",
    ),
    "managed_it": (
        "it support", "managed it", "it services", "it provider",
        "it company", "help desk", "helpdesk", "computer repair",
        "printer", "workstation", "server",
    ),
    "microsoft_365": (
        "microsoft 365", "office 365", "m365", "o365", "outlook",
        "sharepoint", "onedrive", "microsoft teams", "ms teams",
        "exchange online", "azure",
    ),
    "email_and_domains": (
        "business email", "email migration", "email setup", "google workspace",
        "gsuite", "g suite", "email hosting", "mailbox",
    ),
    "networking": (
        "wifi", "wi-fi", "router", "firewall", "vpn", "ethernet",
        "network cabling", "access point", "network setup",
    ),
    "cybersecurity": (
        "cybersecurity", "cyber security", "ransomware", "phishing",
        "malware", "antivirus", "two factor", "2fa", "mfa",
        "password manager", "data breach", "hacked",
    ),
    "backups": (
        "disaster recovery", "data recovery", "backup solution",
        "cloud backup", "offsite backup",
    ),
    "ai_automation": (
        "ai receptionist", "ai answering service", "answering service",
        "ai assistant", "ai agent", "artificial intelligence", "chatgpt",
        "ai tool", "ai automation",
    ),
    "workflow_automation": (
        "automation", "automate", "automating", "workflow", "zapier",
        "make.com", "n8n", "no code", "low code", "auto reply",
        "automated follow up", "follow up sequence", "drip campaign",
        "email sequence", "sms sequence", "text message campaign",
    ),
    "crm": (
        "crm", "gohighlevel", "go high level", "highlevel", "hubspot",
        "salesforce", "pipedrive", "zoho", "keap", "infusionsoft",
        "monday.com", "airtable", "jobber", "housecall pro", "servicetitan",
        "lead tracking", "pipeline management", "job scheduling",
        "scheduling software", "dispatching", "appointment reminder",
        "missed call", "call answering", "quoting software",
    ),
    "integrations": (
        "api", "integration", "integrate", "webhook", "sync between",
        "connect our", "middleware", "single sign on", "sso",
    ),
    "chatbots": (
        "chatbot", "chat bot", "live chat", "messenger bot",
    ),
    "business_process_consulting": (
        "process mapping", "digitize", "digitise", "digital transformation",
        "paperless", "spreadsheet hell", "double entry", "data entry",
        # "still using pen and paper" is BruceTech's clearest digitisation
        # cue, so it counts as an unambiguous service match on its own.
        "pen and paper", "pen & paper",
    ),
}

#: Ambiguous terms. Real BruceTech vocabulary that also appears in ordinary
#: business talk, so they never establish a service match on their own.
WEAK_SERVICE_TERMS: dict[str, tuple[str, ...]] = {
    "managed_it": ("computer", "laptop", "hardware", "device", "tech support"),
    "networking": ("network", "internet", "connectivity"),
    "cybersecurity": ("security", "secure"),
    "backups": ("backup", "backing up"),
    "ecommerce": ("payment", "invoicing", "invoice"),
    "workflow_automation": ("software", "system", "platform", "app", "tool"),
    "crm": (
        "pos", "point of sale", "database", "customer data", "schedule",
        "scheduling", "appointment", "booking", "follow up", "follow-up",
    ),
    "business_process_consulting": ("paperwork", "manual process", "admin"),
}

# ---------------------------------------------------------------------------
# Operational systems pain
#
# A business describing a broken or absent operating system -- inventory that
# does not reconcile, no process in place, a schedule nobody is filling -- is
# describing work BruceTech does, even when it names no software.
#
# The 2026-08-19 fresh run rejected a med spa manager who had "200 units
# missing" and "no systems in place" as UNRELATED with no service match. That
# is a systems engagement described in plain English.
#
# These patterns deliberately describe a *problem*, not a noun. "Inventory"
# alone is a word every retail business uses; "issue with inventory" and
# "inventory discrepancy" are operational failures. They only ever establish
# a match alongside commercial context -- see ``operations_pain_evidence``.
# ---------------------------------------------------------------------------

OPERATIONS_PAIN_PATTERNS: tuple[str, ...] = (
    # Absent systems and process.
    r"no (?:real |proper |actual |good )?(?:systems?|process(?:es)?|procedures?|"
    r"structure|tracking|organization|organisation) (?:in place|at all|here|"
    r"whatsoever)",
    r"(?:without|lack of|lacking) (?:any )?(?:systems?|process(?:es)?|"
    r"procedures?|tracking)",
    r"(?:everything|it'?s all|we'?re) (?:is )?(?:done )?(?:manually|by hand|"
    r"on paper)",
    r"starting from (?:the )?(?:ground up|scratch) (?:with|and)",
    # Inventory and reconciliation failures.
    r"(?:issues?|problems?|trouble|struggling) with (?:our |the |my )?"
    r"(?:inventory|stock|supplies|ordering|scheduling|booking|billing)",
    r"inventory (?:discrepanc|shortage|tracking|count|management|reconcil|"
    r"is a|isn'?t|is not|never)",
    r"(?:can'?t|cannot|unable to|nobody can) (?:tell me |figure out |work out |"
    r"track |account for )?(?:where|what happened to) (?:they|it|them|the \w+) "
    r"(?:went|go)",
    r"(?:\d+ )?(?:units?|products?|items?|stock|inventory|supplies) "
    r"(?:are |is |went |gone )?missing",
    r"missing (?:about |around |roughly |approximately )?\d+ (?:units?|"
    r"products?|items?)",
    r"(?:reconcil\w+|counts?) (?:issues?|problems?|never match|don'?t match|"
    r"do not match|is a nightmare)",
    r"(?:manually|by hand) (?:track|tracking|count|counting|log|logging|"
    r"record|recording|enter|entering)",
    r"no (?:good )?(?:way|system) to (?:track|manage|schedule|organize|"
    r"organise|monitor)",
    # Scheduling, booking, and customer workflow failures.
    r"(?:schedule|calendar|books?|columns?) (?:is|are|stays?|sits?|remains?) "
    r"(?:half )?(?:empty|bare|light|dead|open)",
    r"(?:fill|filling|fill up|filling up) (?:up )?(?:her|his|their|our|the|my) "
    r"(?:schedule|books?|calendar|column|chair)",
    r"struggle (?:to keep|keeping) (?:her|him|them|the \w+) (?:busy|booked)",
    r"(?:keep|keeping) (?:her|him|them) (?:busy|booked)",
    r"(?:business|clinic|shop|practice) (?:that )?is (?:bleeding|hemorrhaging|"
    r"haemorrhaging|a mess|in chaos)",
    r"(?:no.?show|late cancel)\w* (?:are|is|keep|problem|issue|killing)",
    r"(?:double|over).?(?:book|booking|booked)",
)

_OPERATIONS_PAIN_RE = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in OPERATIONS_PAIN_PATTERNS
)

# ---------------------------------------------------------------------------
# Adjacent digital growth
#
# A clinic asking for "a marketing agency to get me patients" is not asking
# for a website. But digital client acquisition is delivered *through* the
# things BruceTech builds: the site, the SEO, the booking flow, the CRM, and
# the follow-up automation behind it.
#
# This is a deliberately narrow bridge, not a service match. It requires an
# explicit provider request AND a digital acquisition goal AND commercial
# context, and it credits only the categories BruceTech would actually
# deliver. The model then decides whether real fit exists.
#
# "Generic marketing" is not enough on its own, and a request that is purely
# for creative or physical-media work is excluded outright.
# ---------------------------------------------------------------------------

ADJACENT_GROWTH_PROVIDER_PATTERNS: tuple[str, ...] = (
    r"marketing (?:agency|agencies|company|companies|firm|team|partner|"
    r"platform|person|guy|help)",
    r"(?:digital|online|internet) marketing",
    r"(?:ads?|advertising) (?:agency|company|manager|management)",
    r"(?:google|facebook|meta|instagram) ads?",
    r"lead gen(?:eration)?",
    r"funnel",
)

#: The outcome must be measurable client acquisition, not exposure.
ADJACENT_GROWTH_GOAL_PATTERNS: tuple[str, ...] = (
    r"(?:get|getting|bring|bringing|drive|driving|attract|attracting|land|"
    r"landing|book|booking|fill|filling)(?: me| us| in| more)*\s*"
    r"(?:new |more |real |actual )*"
    r"(?:patients?|clients?|customers?|leads?|appointments?|bookings?|"
    r"consults?|consultations?)",
    r"(?:new|more|real|actual) (?:patients?|clients?|customers?|leads?|"
    r"appointments?|bookings?)",
    r"(?:grow|growing|build|building|scale|scaling) (?:my|our) "
    r"(?:practice|clinic|business|patient base|client base|book of business)",
    r"(?:client|patient|customer) (?:acquisition|retention|flow)",
    r"(?:fill|filling) (?:up )?(?:my|our|the|her|his|their) "
    r"(?:schedule|books?|calendar|chairs?|rooms?)",
)

#: Creative and physical-media work BruceTech does not sell. A request that
#: names only these has no BruceTech fit, whatever the goal.
NON_BRUCETECH_MARKETING_PATTERNS: tuple[str, ...] = (
    r"influencer",
    r"(?:content|ugc) creator",
    r"photographer|videographer|photo ?shoot|video ?shoot",
    r"\bpr\b|public relations|press release",
    r"(?:brand|branding|logo|rebrand)\w* (?:only|design|designer|identity|"
    r"package|awareness)",
    r"(?:billboard|flyer|flier|print ad|direct mail|radio|magazine|banner "
    r"stand|trade ?show booth)",
)

_ADJACENT_PROVIDER_RE = tuple(
    re.compile(p, re.IGNORECASE) for p in ADJACENT_GROWTH_PROVIDER_PATTERNS
)
_ADJACENT_GOAL_RE = tuple(
    re.compile(p, re.IGNORECASE) for p in ADJACENT_GROWTH_GOAL_PATTERNS
)
_NON_BRUCETECH_MARKETING_RE = tuple(
    re.compile(p, re.IGNORECASE) for p in NON_BRUCETECH_MARKETING_PATTERNS
)

# ---------------------------------------------------------------------------
# Physical and clinical goods
#
# A medical spa group talks constantly about buying lasers, syringes, and
# treatment chairs. Those posts contain words like "system", "platform", and
# "device" that would otherwise corroborate a weak service hit.
#
# BruceTech does not sell, service, or advise on physical clinical equipment.
# When a post is shopping for goods and names no unambiguous BruceTech
# service, the weak-signal path is suppressed entirely.
#
# IT hardware is deliberately absent from this list: "the printer is down"
# and "our workstation crashed" are BruceTech work.
# ---------------------------------------------------------------------------

PHYSICAL_GOODS_PATTERNS: tuple[str, ...] = (
    r"(?:buy|buying|purchas\w+|invest in|investing in|shopping for|"
    r"looking at buying|spend\w* .{0,25} on) (?:a |an |the |some )?"
    r"(?:\w+ ){0,3}(?:laser|device|machine|equipment|chair|bed|unit|"
    r"handpiece|platform|system)\b",
    r"(?:laser|device|machine|equipment|chair|bed|handpiece|syringe|needle|"
    r"cannula)s? (?:for sale|comparison|recommendations?|reviews?)",
    r"(?:which|what) (?:\w+ ){0,2}(?:machine|device|laser|equipment|unit|"
    r"handpiece|syringe|needle|cannula)\b",
    r"\$\s?\d+\s?(?:k|,000)?\b.{0,40}(?:device|machine|equipment|laser)",
    r"(?:botox|dysport|xeomin|jeuveau|neurotoxin|filler|injectable|"
    r"microneedling|skincare ingredient|serum|peel)\b",
    r"(?:tattoo removal|hair removal|body contouring) (?:equipment|device|"
    r"machine|laser|comparison)",
    r"\b(?:syringe|needle|cannula|handpiece|cartridge)s?\b",
)

_PHYSICAL_GOODS_RE = tuple(
    re.compile(p, re.IGNORECASE) for p in PHYSICAL_GOODS_PATTERNS
)


def operations_pain_evidence(text: Any) -> list[str]:
    """Return the operational-systems failures described in a post."""
    body = str(text or "")
    return [p.pattern[:60] for p in _OPERATIONS_PAIN_RE if p.search(body)]


def is_shopping_for_physical_goods(text: Any) -> bool:
    """Is this post shopping for equipment, devices, or clinical product?"""
    body = str(text or "")
    return any(p.search(body) for p in _PHYSICAL_GOODS_RE)


def adjacent_growth_evidence(text: Any) -> list[str]:
    """
    Return evidence that a growth request is one BruceTech can partly serve.

    Requires a provider-shaped marketing ask *and* a measurable client
    acquisition goal. A request naming only creative or physical-media work
    returns nothing, however commercially framed.
    """
    body = str(text or "")

    provider = [p.pattern[:60] for p in _ADJACENT_PROVIDER_RE if p.search(body)]
    goal = [p.pattern[:60] for p in _ADJACENT_GOAL_RE if p.search(body)]

    if not provider or not goal:
        return []

    # Only creative or physical media asked for, and nothing BruceTech
    # builds named alongside it.
    excluded = any(p.search(body) for p in _NON_BRUCETECH_MARKETING_RE)
    if excluded and not match_services_core(body).matched:
        return []

    return provider + goal


#: Terms that make a weak service hit credible. "Our office network keeps
#: dropping" is a managed-IT lead; "grow my network" is not.
TECH_CONTEXT_TERMS: tuple[str, ...] = (
    "set up", "setup", "install", "configure", "configuration", "migrate",
    "migration", "implement", "implementation", "integrate", "integration",
    "troubleshoot", "fix", "broken", "not working", "crashed", "down",
    "upgrade", "rebuild", "build", "digital", "online", "cloud", "office",
    "technician", "technical", "it guy", "computer", "software",
)

#: Everything the AI schema may return, kept aligned with
#: qualification.SERVICE_CATEGORIES.
SERVICE_CATEGORY_NAMES = tuple(
    sorted(set(STRONG_SERVICE_TERMS) | set(WEAK_SERVICE_TERMS))
)

_STRONG_COMPILED = {
    category: _compile_all(terms)
    for category, terms in STRONG_SERVICE_TERMS.items()
}
_WEAK_COMPILED = {
    category: _compile_all(terms)
    for category, terms in WEAK_SERVICE_TERMS.items()
}
_TECH_CONTEXT_COMPILED = _compile_all(TECH_CONTEXT_TERMS)


@dataclass
class ServiceMatch:
    """The deterministic BruceTech service assessment for one post."""

    matched: bool
    categories: list[str] = field(default_factory=list)
    strong_terms: list[str] = field(default_factory=list)
    weak_terms: list[str] = field(default_factory=list)
    has_tech_context: bool = False
    #: Matched through described operational failure rather than vocabulary.
    operations_pain: list[str] = field(default_factory=list)
    #: Matched as an adjacent digital-growth request the model must confirm.
    adjacent_growth: list[str] = field(default_factory=list)
    #: Weak-signal path suppressed because the post is shopping for goods.
    physical_goods: bool = False

    @property
    def strong(self) -> bool:
        """True when at least one unambiguous service term was found."""
        return bool(self.strong_terms)

    @property
    def needs_ai_confirmation(self) -> bool:
        """
        True when the match is inferred rather than stated.

        These reach the model precisely so it can rule on real fit; they are
        never treated as an established service match on their own.
        """
        return bool(self.adjacent_growth) and not self.strong_terms


def match_services_core(text: Any) -> ServiceMatch:
    """
    Determine whether a post names a BruceTech service.

    The rule, in full:

    * **One strong term matches.** "wordpress", "crm", "api", "microsoft 365"
      are unambiguous, so a single hit is enough.
    * **Two or more distinct weak categories match.** "our payment system"
      spans two weak categories and reads as a real systems problem.
    * **One weak term plus technical context.** "office network keeps
      dropping" pairs a weak term with a support-shaped verb.

    The weak paths are suppressed entirely when the post is shopping for
    physical or clinical goods. "Looking at buying a laser platform for a
    small clinic" contains "platform" and "clinic" and is not BruceTech work.

    Anything else is not a service match here, no matter how sympathetic the
    post. See ``match_services`` for the two inferred paths layered on top.
    """
    body = str(text or "")
    if not body.strip():
        return ServiceMatch(matched=False)

    strong_terms: list[str] = []
    weak_terms: list[str] = []
    categories: list[str] = []
    weak_categories: list[str] = []

    for category, compiled in _STRONG_COMPILED.items():
        found = _hits(body, compiled)
        if found:
            strong_terms.extend(found)
            if category not in categories:
                categories.append(category)

    for category, compiled in _WEAK_COMPILED.items():
        found = _hits(body, compiled)
        if found:
            weak_terms.extend(found)
            if category not in weak_categories:
                weak_categories.append(category)

    has_tech_context = bool(_hits(body, _TECH_CONTEXT_COMPILED))
    physical_goods = is_shopping_for_physical_goods(body)

    matched = bool(strong_terms)

    if not matched and weak_terms and not physical_goods:
        # A weak signal needs corroboration: either breadth (two separate
        # weak areas) or a technical verb that makes it a support request.
        matched = len(weak_categories) >= 2 or has_tech_context

    if matched and not strong_terms:
        # Credit the weak categories only when they carried the match.
        for category in weak_categories:
            if category not in categories:
                categories.append(category)

    return ServiceMatch(
        matched=matched,
        categories=categories,
        strong_terms=strong_terms,
        weak_terms=weak_terms,
        has_tech_context=has_tech_context,
        physical_goods=physical_goods,
    )


#: What BruceTech would actually deliver for a business with no systems.
OPERATIONS_PAIN_CATEGORIES = ("business_process_consulting", "workflow_automation")

#: What BruceTech would actually deliver against a client-acquisition goal.
ADJACENT_GROWTH_CATEGORIES = (
    "website_development", "seo", "crm", "workflow_automation",
)


def match_services(text: Any) -> ServiceMatch:
    """
    Determine whether a post credibly matches a BruceTech service.

    Three ways to match, in order:

    1. **Named** -- ``match_services_core``: the post uses BruceTech
       vocabulary.
    2. **Described** -- the post describes an operational systems failure
       (inventory that does not reconcile, no process in place, a schedule
       nobody can fill) from a business with credible commercial context. The
       author never says "software"; they are still describing a systems
       engagement.
    3. **Adjacent** -- the post asks for a provider to deliver measurable
       client acquisition, which BruceTech serves through the site, SEO,
       booking flow, CRM, and follow-up automation underneath it.

    Paths 2 and 3 both require commercial context, and neither produces a
    strong match: they credit the categories BruceTech would deliver and lean
    on the model for the final commercial judgement. Path 3 is flagged
    ``needs_ai_confirmation`` so nothing downstream mistakes it for a stated
    requirement.
    """
    body = str(text or "")
    core = match_services_core(body)

    if core.matched:
        return core

    if not _hits(body, _OPERATOR_COMPILED):
        # Neither inferred path is available unless the author runs the
        # business. A customer describing the same failure is not a lead.
        return core

    operations = operations_pain_evidence(body)
    adjacent = adjacent_growth_evidence(body)

    if not operations and not adjacent:
        return core

    categories = list(core.categories)

    if operations:
        for category in OPERATIONS_PAIN_CATEGORIES:
            if category not in categories:
                categories.append(category)

    if adjacent:
        for category in ADJACENT_GROWTH_CATEGORIES:
            if category not in categories:
                categories.append(category)

    return ServiceMatch(
        matched=True,
        categories=categories,
        strong_terms=[],
        weak_terms=core.weak_terms,
        has_tech_context=core.has_tech_context,
        operations_pain=operations,
        adjacent_growth=adjacent,
        physical_goods=core.physical_goods,
    )


# ---------------------------------------------------------------------------
# Intent vocabulary
#
# Order matters: classify_intent applies these groups in sequence and returns
# the first match, so the more commercially specific patterns come first.
# ---------------------------------------------------------------------------

#: Someone is explicitly looking for a *person or company* to hire.
#:
#: These deliberately require a provider noun. "need help" alone is not a
#: provider request — that is an implementation request at most.
#: The nouns that turn a request into a request *for a provider*. Kept as one
#: shared fragment so every provider pattern stays consistent.
#
# "business" and "people" are deliberately absent. "need business funding"
# and "need more business" are not requests for a provider, and including
# those nouns made an excavation-financing post look like a hiring request.
_PROVIDER_NOUNS = (
    r"(?:some ?one|some ?body|person|company|companies|"
    r"agency|agencies|firm|freelancer|contractor|developer|dev\b|designer|"
    r"consultant|expert|specialist|professional|provider|vendor|technician|"
    r"guy|gal|team|shop|pro\b|programmer|engineer)"
)

#: Optional articles, adjectives, and one domain modifier between the verb and
#: the provider noun, so "a good web developer" and "an IT company" match as
#: readily as a bare "someone".
_PROVIDER_FILLER = (
    r"(?:\s*(?:a|an|any|some|the))?"
    r"(?:\s*(?:good|great|reliable|local|trusted|experienced|decent|solid|"
    r"affordable|new|second|another))*\s*"
    r"(?:[a-z0-9.+#-]{2,15}\s+)?"
)

PROVIDER_REQUEST_PATTERNS: tuple[str, ...] = (
    rf"look(?:ing)? (?:for|to find){_PROVIDER_FILLER}{_PROVIDER_NOUNS}",
    r"look(?:ing)? to (?:hire|outsource|contract|engage|pay some ?one)",
    rf"(?:need|needs|want|wants|seeking|searching for|in search of|after)"
    rf"{_PROVIDER_FILLER}{_PROVIDER_NOUNS}",
    rf"any(?:one|body) (?:know|knows|recommend|recommends|suggest|suggests)"
    rf"(?:\s*of)?{_PROVIDER_FILLER}{_PROVIDER_NOUNS}",
    r"who (?:do|would|can|should|does) (?:you|everyone|anyone|people|i|we)"
    r"(?: all)? (?:recommend|use|suggest|go with|hire)",
    rf"recommendations? for{_PROVIDER_FILLER}{_PROVIDER_NOUNS}",
    rf"(?:hire|hiring){_PROVIDER_FILLER}{_PROVIDER_NOUNS}",
    r"(?:need|want|ready|looking) to hire",
    r"(?:taking|accepting|open to|collecting) (?:on )?(?:quotes|bids|proposals|"
    r"estimates)",
    r"(?:send|dm|pm) me (?:your|a) (?:quote|rate|pricing|portfolio)",
    rf"\biso\b{_PROVIDER_FILLER}{_PROVIDER_NOUNS}",
)

#: Someone wants a specific thing built, set up, migrated, or connected.
IMPLEMENTATION_REQUEST_PATTERNS: tuple[str, ...] = (
    r"need (?:help |someone |a hand )?(?:to |with )?(?:set(?:ting)? ?up|setup|install|"
    r"configur|implement|migrat|integrat|connect|build|rebuild|redesign|automat)",
    r"(?:help|assistance) (?:with |to )?(?:set(?:ting)? ?up|setup|install|configur|"
    r"implement|migrat|integrat|connect|build|rebuild|redesign|automat)",
    r"(?:want|need|trying|looking) to (?:set ?up|install|configure|implement|migrate|"
    r"integrate|connect|build|rebuild|redesign|automate|streamline|digitize|digitise)",
    r"need (?:a |an |our |my |the )?[a-z ]{0,30}?(?:built|rebuilt|redesigned|migrated|"
    r"configured|connected|integrated|automated|set ?up|installed|fixed|upgraded)",
    r"(?:get|getting) (?:a |an |our |my |the )?[a-z ]{0,30}?(?:built|rebuilt|set ?up|"
    r"migrated|connected|integrated|automated)",
    r"(?:can someone|could someone|is there someone who can) (?:help|build|set|fix)",
    r"the whole [a-z ]{0,20}(?:setup|set ?up|build|implementation)",
    r"(?:needs|need) (?:to be |to get )?(?:built|rebuilt|redone|replaced|migrated)",
)

#: Someone is asking which product to buy, not who should build it.
TOOL_RESEARCH_PATTERNS: tuple[str, ...] = (
    r"(?:what|which|whats|what's) (?:kind of |type of |sort of )?"
    r"(?:crm|software|system|platform|app|tool|program|pos|service)",
    r"(?:what|which) (?:.{0,40}?)(?:are|do) (?:you|everyone|others|people|"
    r"other \w+|folks|y'all|yall)(?: all)? (?:use|using|recommend|prefer|like)",
    r"(?:has|have) any(?:one|body) (?:tried|used|tested|switched to)",
    r"does any(?:one|body) use\b",
    r"any(?:one|body) (?:using|tried)\b",
    r"thoughts on (?:using )?[a-z0-9.]+\?",
    r"(?:pros and cons|comparison) (?:of|between)",
    r"(?:worth it|any good)\?",
    # "Mangomint or boulevard POS system and why?" -- a named either/or over
    # a software noun. The software noun is required: "PicoWay or PicoSure
    # laser" is equipment shopping, not tool research.
    r"\b[a-z][\w.']{2,}\s+(?:or|vs\.?|versus)\s+[a-z][\w.']{2,}"
    r"(?:\s+\w+){0,2}\s+(?:pos|crm|software|system|platform|app|suite|"
    r"booking system|scheduling software|point of sale)\b",
    # "thinking of switching to either Mangomint or GlossGenius"
    r"(?:think(?:ing)?|considering|looking) (?:about |of |at )?"
    r"(?:switch(?:ing)?|mov(?:e|ing)|migrat(?:e|ing)|chang(?:e|ing))"
    r"(?: over)? to\b",
    r"(?:currently|we|they) (?:use|using|are on|'re on|run|running)\s+"
    r"[a-z][\w.']{2,}\b.{0,80}?(?:switch|instead|alternative|replace|"
    r"pros and cons|thoughts)",
    r"\bpros and cons\b",
    r"(?:switch(?:ing)?|migrat(?:e|ing)) (?:from|off) [a-z][\w.']{2,}",
)

#: Operational pain that BruceTech's automation and IT work addresses.
BUSINESS_PAIN_PATTERNS: tuple[str, ...] = (
    r"missing (?:calls|customers|leads|appointments|bookings)",
    r"(?:can'?t|cannot|unable to) keep up with (?:calls|emails|messages|bookings|admin)",
    r"(?:too much|so much|drowning in|buried in|swamped with) "
    r"(?:manual |admin|paperwork|data entry|busywork|back and forth)",
    r"(?:still|currently) (?:use|uses|using|on|rely on|doing|do) ?(?:a )?"
    r"(?:pen and paper|pen & paper|paper|spreadsheets?|excel|whiteboard|"
    r"sticky notes|notebook)",
    r"(?:customers|clients|people) (?:keep )?(?:forgetting|no.?showing|not showing)",
    r"manually (?:following up|entering|copying|tracking|scheduling|texting|emailing)",
    r"(?:answering|returning|taking|fielding) (?:calls|texts|messages|emails)"
    r"[a-z ,&]{0,25}?(?:all|every|late|until|into the) "
    r"(?:evening|night|weekend|day|hours)",
    r"(?:website|site|system|server|computer|network) (?:keeps? |is |has been )"
    r"(?:break|breaking|crashing|going down|down|broken|slow)",
    r"(?:scheduling|admin|paperwork|follow.?up|bookkeeping) is "
    r"(?:overwhelming|a nightmare|killing me|out of control|taking over)",
    r"(?:falling|slipping) through the cracks",
    r"double.?book",
    r"(?:losing|lost) (?:business|customers|clients|leads|jobs) because",
    r"(?:spending|wasting) (?:hours|so long|too long|all day)",
    r"no (?:good )?(?:way|system) to (?:track|manage|schedule|organize|organise)",
)

#: Business questions with no BruceTech connection. Explicitly listed so a
#: financing or hiring question is never mistaken for a technology need.
GENERAL_ADVICE_PATTERNS: tuple[str, ...] = (
    r"(?:business |working |equipment )?(?:financ(?:e|ing)|funding|loan|lending|"
    r"line of credit|capital|investor|grant|lease|leasing|mortgage)",
    r"how (?:do|did|can|should) (?:i|we|you) (?:get|find|secure|raise|qualify for)"
    r" (?:a |an )?(?:loan|financing|funding|capital|grant|investor)",
    r"should i (?:hire|fire|lay off|take on) (?:an? )?(?:employee|staff|worker|"
    r"apprentice|helper|subcontractor)",
    r"how (?:much|many) (?:inventory|stock|product|material)",
    r"(?:what|which|who) (?:supplier|distributor|wholesaler|manufacturer)",
    r"how (?:should|do) (?:i|we|you) price",
    r"(?:pricing|rates?) for (?:my|our|your) (?:service|work|labour|labor)",
    r"(?:insurance|accountant|bookkeeper|lawyer|licence|license|permit|"
    r"incorporat|payroll|tax(?:es)?|wsib)",
    r"(?:best|good) (?:truck|trailer|excavator|machine|equipment|vehicle)",
)


def _compile_patterns(patterns: Iterable[str]) -> tuple[Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


_PROVIDER_RE = _compile_patterns(PROVIDER_REQUEST_PATTERNS)
_IMPLEMENTATION_RE = _compile_patterns(IMPLEMENTATION_REQUEST_PATTERNS)
_TOOL_RESEARCH_RE = _compile_patterns(TOOL_RESEARCH_PATTERNS)
_BUSINESS_PAIN_RE = _compile_patterns(BUSINESS_PAIN_PATTERNS)
_GENERAL_ADVICE_RE = _compile_patterns(GENERAL_ADVICE_PATTERNS)


def _matched_patterns(
    text: str,
    patterns: tuple[Pattern[str], ...],
) -> list[str]:
    return [
        pattern.pattern[:60]
        for pattern in patterns
        if pattern.search(text)
    ]


@dataclass
class IntentResult:
    """The deterministic intent assessment for one post."""

    intent_type: str
    label: str
    evidence: list[str] = field(default_factory=list)

    @property
    def is_ai_eligible(self) -> bool:
        return self.intent_type in AI_ELIGIBLE_INTENTS

    @property
    def allows_outreach(self) -> bool:
        return self.intent_type in OUTREACH_INTENTS


def classify_intent(text: Any) -> IntentResult:
    """
    Classify what the author is asking for.

    Evaluated in priority order — provider, implementation, tool research,
    business pain, general advice — and the first match wins.

    Tool research is checked *before* business pain so "what CRM does everyone
    use because I keep missing calls?" is treated as research, not as a lead.
    Provider and implementation both outrank it, because "looking for someone
    to set up a CRM" is a hiring request that happens to name a product.

    This function says nothing about whether BruceTech can help. That is
    ``match_services``' job, and the prefilter requires both.
    """
    body = str(text or "").strip()
    if not body:
        return IntentResult(
            intent_type=INTENT_UNRELATED,
            label=INTENT_LABELS[INTENT_UNRELATED],
        )

    for intent_type, patterns in (
        (INTENT_PROVIDER_REQUEST, _PROVIDER_RE),
        (INTENT_IMPLEMENTATION_REQUEST, _IMPLEMENTATION_RE),
        (INTENT_TOOL_RESEARCH, _TOOL_RESEARCH_RE),
        (INTENT_BUSINESS_PAIN, _BUSINESS_PAIN_RE),
    ):
        evidence = _matched_patterns(body, patterns)
        if evidence:
            return IntentResult(
                intent_type=intent_type,
                label=INTENT_LABELS[intent_type],
                evidence=evidence,
            )

    # Operational systems failure described in plain English, from a business
    # with credible commercial context. "Two hundred units missing and no
    # systems in place" names no software and is still operational pain.
    #
    # Checked after the explicit pain patterns and before general advice, and
    # gated on commercial context so a consumer grumble stays UNRELATED.
    operations = operations_pain_evidence(body)
    if operations and _hits(body, _OPERATOR_COMPILED):
        return IntentResult(
            intent_type=INTENT_BUSINESS_PAIN,
            label=INTENT_LABELS[INTENT_BUSINESS_PAIN],
            evidence=operations,
        )

    advice = _matched_patterns(body, _GENERAL_ADVICE_RE)
    if advice:
        return IntentResult(
            intent_type=INTENT_GENERAL_ADVICE,
            label=INTENT_LABELS[INTENT_GENERAL_ADVICE],
            evidence=advice,
        )

    return IntentResult(
        intent_type=INTENT_UNRELATED,
        label=INTENT_LABELS[INTENT_UNRELATED],
    )


# ---------------------------------------------------------------------------
# Self-promotion, job seeking, and resolved-request detection
#
# These mirror the Airtable Promotion Signal formula. Python must not depend
# on the formula having calculated.
# ---------------------------------------------------------------------------

#: Unambiguous seller-to-audience evidence. One hit is enough: these are
#: calls to action, solicitations, and announcements, not description.
PROMOTIONAL_TERMS: tuple[str, ...] = (
    "dm me", "pm me", "inbox me", "hire me", "message me for",
    "message me if", "shoot me a message", "reach out to me",
    "book a call with", "book now", "book your", "call us today",
    "contact us", "contact me for",
    "limited time", "promo code", "discount code", "special offer",
    "sign up now", "click the link", "link in bio", "affiliate",
    "for sale", "available now", "now booking",
    "accepting new clients", "taking on new clients", "free consultation",
    "free audit", "check out my", "check out our", "proud to announce",
    "packages start", "pricing starts", "our rates",
)

#: Self-description that is promotional *only alongside* a call to action.
#:
#: "We offer facials, DiamondGlow and SkinPen but her books are half empty"
#: is a business owner giving context for a problem. Treating that as
#: self-promotion rejected a real lead before the model ever saw it, which is
#: exactly what happened to a med spa owner in the 2026-08-19 fresh run.
#:
#: These establish that the author sells something. A CTA from
#: PROMOTIONAL_TERMS establishes that they are selling it *here*, to this
#: audience. Promotion requires both.
PROMOTIONAL_SELF_DESCRIPTION_TERMS: tuple[str, ...] = (
    "i offer", "we offer", "my services", "our services", "we provide",
    "i provide", "we specialize", "we specialise", "our agency",
    "my agency", "we sell", "i sell", "selling", "we carry",
)

JOB_SEEKER_TERMS: tuple[str, ...] = (
    "looking for work", "looking for a job", "seeking employment",
    "job seeker", "resume attached", "cv attached", "available for hire",
    "open to work", "any openings", "hiring me", "i am available",
    "seeking a position", "looking for opportunities",
)

FREE_ONLY_TERMS: tuple[str, ...] = (
    "free of charge", "for free", "no budget", "pro bono", "unpaid",
    "cheapest possible", "student project", "school project", "assignment",
    "homework", "thesis", "class project",
)

RESOLVED_TERMS: tuple[str, ...] = (
    "found someone", "sorted now", "issue resolved", "already fixed",
    "thanks everyone", "no longer needed", "all set now", "problem solved",
    "we went with", "decided to go with", "hired someone", "got it working",
)

_PROMOTIONAL_COMPILED = _compile_all(PROMOTIONAL_TERMS)
_PROMOTIONAL_SELF_COMPILED = _compile_all(PROMOTIONAL_SELF_DESCRIPTION_TERMS)
_JOB_SEEKER_COMPILED = _compile_all(JOB_SEEKER_TERMS)
_FREE_ONLY_COMPILED = _compile_all(FREE_ONLY_TERMS)
_RESOLVED_COMPILED = _compile_all(RESOLVED_TERMS)

#: Language that establishes the author runs a business.
COMMERCIAL_CONTEXT_TERMS: tuple[str, ...] = (
    "my business", "our business", "my company", "our company", "my shop",
    "our shop", "my store", "our store", "my clients", "our clients",
    "my customers", "our customers", "my staff", "our staff", "my team",
    "our team", "i own", "we own", "i run", "we run", "my crew", "our crew",
    "small business", "family business", "our office", "my office",
    "my employees", "our employees", "franchise", "storefront",
    "contractor", "electrician", "plumber", "hvac", "roofer", "landscaper",
    "restaurant", "clinic", "dental", "salon", "gym", "law firm",
    "real estate", "brokerage", "dealership", "excavation", "construction",
    # Medical spa and practice framings, added after the 2026-08-19 fresh
    # run: an owner writing "I recently opened my own med spa" is giving
    # business context as plainly as "I own a restaurant".
    "med spa", "medspa", "medical spa", "my practice", "our practice",
    "solo md", "my clinic", "our clinic", "practice owner", "clinic owner",
    "spa owner", "studio owner", "new manager at",
)

_COMMERCIAL_COMPILED = _compile_all(COMMERCIAL_CONTEXT_TERMS)

#: The subset of commercial context that establishes the author *runs* the
#: business, rather than merely naming one.
#:
#: COMMERCIAL_CONTEXT_TERMS includes bare industry nouns -- "salon", "clinic",
#: "gym" -- which a customer uses as readily as an owner. That is fine for
#: scoring, where it is one weighted signal among several. It is not fine as
#: the gate on an inferred match: "my salon appointment was double booked and
#: nobody could tell me where my order went" is a customer complaint that
#: otherwise reads as operational pain from a business.
#:
#: The two inferred service paths gate on this list instead.
OPERATOR_CONTEXT_TERMS: tuple[str, ...] = (
    "my business", "our business", "my company", "our company", "my shop",
    "our shop", "my store", "our store", "my clients", "our clients",
    "my customers", "our customers", "my staff", "our staff", "my team",
    "our team", "i own", "we own", "i run", "we run", "my crew", "our crew",
    "our office", "my office", "my employees", "our employees",
    "my practice", "our practice", "my clinic", "our clinic",
    "solo md", "practice owner", "clinic owner", "spa owner",
    "studio owner", "business owner", "owner here", "new manager at",
    "i manage", "we manage", "i opened", "we opened", "opened my own",
    "opened our own", "i started", "we started",
)

_OPERATOR_COMPILED = _compile_all(OPERATOR_CONTEXT_TERMS)


def operator_context_terms(text: Any) -> list[str]:
    """Return the phrases showing the author runs the business."""
    return _hits(str(text or ""), _OPERATOR_COMPILED)


@dataclass
class NegativeSignals:
    """Deterministic red flags found in a post."""

    promotional: list[str] = field(default_factory=list)
    job_seeking: list[str] = field(default_factory=list)
    free_only: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    #: Self-description found without a call to action. Recorded for the
    #: diagnostics blob only -- it is not a rejection on its own.
    self_description: list[str] = field(default_factory=list)

    @property
    def any_present(self) -> bool:
        return bool(
            self.promotional or self.job_seeking or self.free_only or self.resolved
        )


def detect_negative_signals(text: Any) -> NegativeSignals:
    """
    Find promotional, job-seeking, free-only, and resolved language.

    Promotion needs seller-to-audience evidence, not merely evidence that the
    author sells something. "We offer facials" describes a business;
    "we offer facials, DM me to book" advertises one. Only the second is
    promotional, and only the second sets ``promotional``.
    """
    body = str(text or "")

    call_to_action = _hits(body, _PROMOTIONAL_COMPILED)
    self_description = _hits(body, _PROMOTIONAL_SELF_COMPILED)

    promotional = list(call_to_action)
    if call_to_action and self_description:
        # A seller describing their offer *and* soliciting. Report both so
        # the Airtable reason names what was actually found.
        promotional.extend(
            term for term in self_description if term not in promotional
        )

    return NegativeSignals(
        promotional=promotional,
        job_seeking=_hits(body, _JOB_SEEKER_COMPILED),
        free_only=_hits(body, _FREE_ONLY_COMPILED),
        resolved=_hits(body, _RESOLVED_COMPILED),
        self_description=self_description,
    )


def commercial_context_terms(text: Any) -> list[str]:
    """Return the business-context phrases present in a post."""
    return _hits(str(text or ""), _COMMERCIAL_COMPILED)


# ---------------------------------------------------------------------------
# General-advice subtypes
#
# Used to record *why* a general business question was dropped, so the
# Airtable Disqualifiers column reads "FUNDING_OR_FINANCE_REQUEST" rather than
# a catch-all.
# ---------------------------------------------------------------------------

FUNDING_TERMS: tuple[str, ...] = (
    "financing", "finance", "funding", "loan", "lender", "lending",
    "line of credit", "working capital", "capital", "investor", "investment",
    "grant", "leasing", "lease", "mortgage", "down payment", "credit score",
)

HIRING_TERMS: tuple[str, ...] = (
    "hire an employee", "hire someone full time", "hiring staff",
    "should i hire", "employee", "apprentice", "subcontractor", "payroll",
    "labour", "labor", "crew member", "wages", "salary",
)

_FUNDING_COMPILED = _compile_all(FUNDING_TERMS)
_HIRING_COMPILED = _compile_all(HIRING_TERMS)


def general_advice_subtype(text: Any) -> str | None:
    """
    Classify a general-advice post as a funding or staffing question.

    Returns ``"FUNDING"``, ``"HIRING"``, or ``None`` when it is neither.
    Funding wins ties: "should I get a loan or hire someone" is primarily a
    financing question.
    """
    body = str(text or "")
    if not body.strip():
        return None

    if _hits(body, _FUNDING_COMPILED):
        return "FUNDING"

    if _hits(body, _HIRING_COMPILED):
        return "HIRING"

    return None
