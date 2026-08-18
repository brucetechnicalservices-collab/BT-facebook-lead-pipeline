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

    @property
    def strong(self) -> bool:
        """True when at least one unambiguous service term was found."""
        return bool(self.strong_terms)


def match_services(text: Any) -> ServiceMatch:
    """
    Determine whether a post credibly matches a BruceTech service.

    The rule, in full:

    * **One strong term matches.** "wordpress", "crm", "api", "microsoft 365"
      are unambiguous, so a single hit is enough.
    * **Two or more distinct weak categories match.** "our payment system"
      spans two weak categories and reads as a real systems problem.
    * **One weak term plus technical context.** "office network keeps
      dropping" pairs a weak term with a support-shaped verb.

    Anything else is not a service match, no matter how sympathetic the post.
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

    matched = bool(strong_terms)

    if not matched and weak_terms:
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
        (INTENT_GENERAL_ADVICE, _GENERAL_ADVICE_RE),
    ):
        evidence = _matched_patterns(body, patterns)
        if evidence:
            return IntentResult(
                intent_type=intent_type,
                label=INTENT_LABELS[intent_type],
                evidence=evidence,
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

PROMOTIONAL_TERMS: tuple[str, ...] = (
    "dm me", "pm me", "inbox me", "hire me", "message me for", "i offer",
    "we offer", "my services", "our services", "we provide", "we specialize",
    "we specialise", "our agency", "my agency", "book a call with",
    "limited time", "promo code", "discount code", "sign up now",
    "click the link", "link in bio", "affiliate", "for sale", "now booking",
    "accepting new clients", "taking on new clients", "free consultation",
    "free audit", "check out my", "check out our", "proud to announce",
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
)

_COMMERCIAL_COMPILED = _compile_all(COMMERCIAL_CONTEXT_TERMS)


@dataclass
class NegativeSignals:
    """Deterministic red flags found in a post."""

    promotional: list[str] = field(default_factory=list)
    job_seeking: list[str] = field(default_factory=list)
    free_only: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    @property
    def any_present(self) -> bool:
        return bool(
            self.promotional or self.job_seeking or self.free_only or self.resolved
        )


def detect_negative_signals(text: Any) -> NegativeSignals:
    """Find promotional, job-seeking, free-only, and resolved language."""
    body = str(text or "")

    return NegativeSignals(
        promotional=_hits(body, _PROMOTIONAL_COMPILED),
        job_seeking=_hits(body, _JOB_SEEKER_COMPILED),
        free_only=_hits(body, _FREE_ONLY_COMPILED),
        resolved=_hits(body, _RESOLVED_COMPILED),
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
