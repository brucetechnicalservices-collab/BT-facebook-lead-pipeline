"""
Deterministic lead qualification for the BruceTech Facebook pipeline.

The AI model extracts structured *signals* only. Every decision that
determines whether a lead reaches a human -- the numeric score, the tier,
hard rejections, the Qualified flag, and outreach eligibility -- is computed
here in Python so the outcome is reproducible and testable.

This module is intentionally free of environment variables, network calls,
and third-party imports so it can be imported by tests without any
credentials present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import intent
from normalization import sanitize_outreach_copy

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: A lead must reach this score to be marked Qualified.
DEFAULT_QUALIFICATION_THRESHOLD = 65

#: Scores at or above this value but below the qualification threshold are
#: routed to a human instead of being discarded.
DEFAULT_MANUAL_REVIEW_THRESHOLD = 55

#: Scores at or above this value are the strongest tier.
DEFAULT_HOT_THRESHOLD = 80

#: Posts older than this are hard-rejected regardless of score.
#:
#: Lowered from 45 to 5. The scraper runs on roughly a 3-day window, and a
#: Facebook request older than a work week has almost always been answered
#: already. Override with MAX_POST_AGE_DAYS when working a backlog.
DEFAULT_MAX_POST_AGE_DAYS = 5

#: Minimum classification_confidence before automatic outreach is allowed.
DEFAULT_MIN_OUTREACH_CONFIDENCE = 0.55

#: BUSINESS_PAIN leads must clear this score before automatic outreach.
#: Someone describing a problem has not asked for a provider, so the bar is
#: higher than the plain qualification threshold.
DEFAULT_BUSINESS_PAIN_OUTREACH_SCORE = 70

# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

TIER_HOT = "Hot"
TIER_QUALIFIED = "Qualified"
TIER_MANUAL_REVIEW = "Manual Review"
TIER_REJECTED = "Rejected"

#: Only these tiers may ever set Qualified = true.
QUALIFYING_TIERS = frozenset({TIER_HOT, TIER_QUALIFIED})

CHANNEL_DIRECT_MESSAGE = "direct_message"
CHANNEL_PUBLIC_REPLY_THEN_DM = "public_reply_then_dm"
CHANNEL_DO_NOT_CONTACT = "do_not_contact"

#: Channels that represent a real outreach action.
OUTREACH_CHANNELS = frozenset(
    {CHANNEL_DIRECT_MESSAGE, CHANNEL_PUBLIC_REPLY_THEN_DM}
)

# ---------------------------------------------------------------------------
# Hard rejection codes
#
# A hard rejection always overrides the numeric score. A post that trips any
# of these is Rejected even with a score of 100.
# ---------------------------------------------------------------------------

REJECT_PERSONAL_REQUEST = "PERSONAL_REQUEST"
REJECT_PROMOTIONAL_POST = "PROMOTIONAL_POST"
REJECT_COMPETITOR_OR_AGENCY = "COMPETITOR_OR_AGENCY"
REJECT_JOB_SEEKER = "JOB_SEEKER"
REJECT_SPAM_OR_AFFILIATE = "SPAM_OR_AFFILIATE"
REJECT_STUDENT_OR_EDUCATIONAL = "STUDENT_OR_EDUCATIONAL"
REJECT_FREE_ONLY_REQUEST = "FREE_ONLY_REQUEST"
REJECT_ALREADY_RESOLVED = "ALREADY_RESOLVED"
REJECT_PROVIDER_SELECTED = "PROVIDER_ALREADY_SELECTED"
REJECT_NO_BUSINESS_CONTEXT = "NO_BUSINESS_CONTEXT"
REJECT_NO_SERVICE_MATCH = "NO_SERVICE_MATCH"
REJECT_INAPPROPRIATE_OUTREACH = "INAPPROPRIATE_OUTREACH"
REJECT_STALE_POST = "STALE_POST"

# Added by the intent release. These are decided in Python from the
# deterministic intent classifier and pipeline configuration, never by the
# model, so they are not offered to it in the JSON schema.
REJECT_NO_BUYING_INTENT = "NO_BUYING_INTENT"
REJECT_FUNDING_REQUEST = "FUNDING_OR_FINANCE_REQUEST"
REJECT_HIRING_UNRELATED = "HIRING_UNRELATED"
REJECT_SUPPRESSED_AUTHOR = "SUPPRESSED_AUTHOR"
REJECT_DISALLOWED_GROUP = "DISALLOWED_GROUP"
REJECT_HUMAN_REJECTED = "HUMAN_REJECTED"
REJECT_POST_TOO_SHORT = "POST_TOO_SHORT"

HARD_REJECTION_REASONS: dict[str, str] = {
    REJECT_PERSONAL_REQUEST: (
        "Personal consumer request rather than a business need."
    ),
    REJECT_PROMOTIONAL_POST: (
        "Promotional post from a service provider advertising itself."
    ),
    REJECT_COMPETITOR_OR_AGENCY: (
        "Agency, freelancer, or competitor advertising their own services."
    ),
    REJECT_JOB_SEEKER: (
        "Job seeker or person offering their labour."
    ),
    REJECT_SPAM_OR_AFFILIATE: (
        "Spam or affiliate promotion."
    ),
    REJECT_STUDENT_OR_EDUCATIONAL: (
        "Student or educational question rather than a commercial need."
    ),
    REJECT_FREE_ONLY_REQUEST: (
        "Request explicitly asks for free work only."
    ),
    REJECT_ALREADY_RESOLVED: (
        "The request has already been resolved."
    ),
    REJECT_PROVIDER_SELECTED: (
        "A provider has already been selected."
    ),
    REJECT_NO_BUSINESS_CONTEXT: (
        "No credible business or organizational context."
    ),
    REJECT_NO_SERVICE_MATCH: (
        "No match with any BruceTech service."
    ),
    REJECT_INAPPROPRIATE_OUTREACH: (
        "Outreach would not be appropriate for this post."
    ),
    REJECT_STALE_POST: (
        "Post is older than the configured maximum age."
    ),
    REJECT_NO_BUYING_INTENT: (
        "General advice or unrelated discussion, not a request BruceTech "
        "can serve."
    ),
    REJECT_FUNDING_REQUEST: (
        "Financing, funding, or credit request with no BruceTech service "
        "need."
    ),
    REJECT_HIRING_UNRELATED: (
        "Hiring request unrelated to any BruceTech service."
    ),
    REJECT_SUPPRESSED_AUTHOR: (
        "Author is on the suppression list."
    ),
    REJECT_DISALLOWED_GROUP: (
        "Post came from a group excluded from outreach."
    ),
    REJECT_HUMAN_REJECTED: (
        "A human reviewer rejected this record."
    ),
    REJECT_POST_TOO_SHORT: (
        "Post text is too short to evaluate."
    ),
}

#: Codes decided in Python. The model is never asked about these, so they are
#: excluded from the JSON schema enum below.
PYTHON_ONLY_DISQUALIFIER_CODES = (
    REJECT_STALE_POST,
    REJECT_NO_BUYING_INTENT,
    REJECT_FUNDING_REQUEST,
    REJECT_HIRING_UNRELATED,
    REJECT_SUPPRESSED_AUTHOR,
    REJECT_DISALLOWED_GROUP,
    REJECT_HUMAN_REJECTED,
    REJECT_POST_TOO_SHORT,
)

# ---------------------------------------------------------------------------
# What a human "Approve" may and may not override
#
# A person setting Human Decision = Approve is saying "the intent heuristic is
# wrong about this post, ask the model anyway". That is the only thing it
# means. It is not a licence to spend a token on an expired post, to contact a
# self-promoting agency, or to re-open a request that is already resolved.
#
# The two sets below are exhaustive and mutually exclusive, and the override is
# applied by *filtering against them* rather than by conditionals scattered
# through the rejection logic. Any code added later is non-overridable until
# someone deliberately lists it as overridable, which is the safe default.
# ---------------------------------------------------------------------------

#: Heuristic intent vetoes. These are the only rejections a human Approve may
#: lift, because they are exactly the guesses a human reviewer can see are
#: wrong by reading the post.
HUMAN_OVERRIDABLE_HARD_REJECTIONS = frozenset(
    {
        REJECT_NO_BUYING_INTENT,
        REJECT_FUNDING_REQUEST,
        REJECT_HIRING_UNRELATED,
    }
)

#: Data-integrity and safety exclusions. No human decision, no Airtable
#: formula, and no configuration flag lifts any of these. STALE_POST is here
#: deliberately: an expired Facebook lead must never consume an AI call, even
#: when someone approved it.
NON_OVERRIDABLE_HARD_REJECTIONS = frozenset(
    code
    for code in HARD_REJECTION_REASONS
    if code not in HUMAN_OVERRIDABLE_HARD_REJECTIONS
)


def apply_human_override(codes: Sequence[str]) -> list[str]:
    """
    Drop the rejection codes a human Approve is allowed to lift.

    Everything in :data:`NON_OVERRIDABLE_HARD_REJECTIONS` survives.
    """
    return [
        code
        for code in codes
        if code not in HUMAN_OVERRIDABLE_HARD_REJECTIONS
    ]

#: disqualifier_codes the model may return that map onto a hard rejection.
KNOWN_DISQUALIFIER_CODES = tuple(
    code
    for code in HARD_REJECTION_REASONS
    if code not in PYTHON_ONLY_DISQUALIFIER_CODES
)

#: Every code the pipeline can emit, model-reported or Python-decided.
ALL_DISQUALIFIER_CODES = tuple(HARD_REJECTION_REASONS.keys())

# ---------------------------------------------------------------------------
# Signal vocabularies and their score contributions
#
# The maximum contributions sum to exactly 100.
# ---------------------------------------------------------------------------

INTENT_STRENGTH_SCORES = {
    "none": 0,
    "weak": 9,
    "moderate": 19,
    "strong": 28,
}

PROBLEM_SPECIFICITY_SCORES = {
    "none": 0,
    "vague": 4,
    "specific": 9,
    "detailed": 12,
}

BUSINESS_CONTEXT_SCORES = {
    "none": 0,
    "unclear": 4,
    "individual_or_sole_trader": 8,
    "small_business": 11,
    "established_business": 12,
}

PURCHASE_SIGNAL_SCORES = {
    "none": 0,
    "researching": 4,
    "comparing_options": 8,
    "ready_to_buy": 10,
}

URGENCY_SCORES = {
    "none": 0,
    "low": 2,
    "medium": 4,
    "high": 6,
}

BUSINESS_IMPACT_SCORES = {
    "none": 0,
    "low": 2,
    "medium": 4,
    "high": 6,
}

LOCATION_SCORES = {
    "unknown": 0,
    "other": 0,
    "canada": 2,
    "ontario": 3,
    "toronto_gta": 3,
}

BUYER_ROLE_SCORES = {
    "unknown": 0,
    "other": 0,
    "employee": 1,
    "manager": 2,
    "owner_or_decision_maker": 3,
}

#: Score for matching at least one BruceTech service, plus a small bonus for
#: additional matched categories.
SERVICE_MATCH_BASE_SCORE = 16
SERVICE_MATCH_PER_EXTRA_CATEGORY = 2
SERVICE_MATCH_MAX_SCORE = 20

MAX_COMPONENT_SCORE = (
    max(INTENT_STRENGTH_SCORES.values())
    + max(PROBLEM_SPECIFICITY_SCORES.values())
    + max(BUSINESS_CONTEXT_SCORES.values())
    + max(PURCHASE_SIGNAL_SCORES.values())
    + max(URGENCY_SCORES.values())
    + max(BUSINESS_IMPACT_SCORES.values())
    + max(LOCATION_SCORES.values())
    + max(BUYER_ROLE_SCORES.values())
    + SERVICE_MATCH_MAX_SCORE
)

# Soft penalties. These reduce the score but never veto on their own.
PENALTY_LIKELY_RESOLVED = 15
PENALTY_SPAM_RISK_MEDIUM = 12
PENALTY_SPAM_RISK_LOW = 4
PENALTY_BORDERLINE_OUTREACH = 10
PENALTY_LOW_CONFIDENCE = 8

#: Below this confidence the score is penalised.
LOW_CONFIDENCE_CUTOFF = 0.5

RESOLVED_STATUS_VALUES = ("unresolved", "likely_resolved", "resolved")
SPAM_RISK_VALUES = ("none", "low", "medium", "high")
OUTREACH_APPROPRIATENESS_VALUES = (
    "appropriate",
    "borderline",
    "inappropriate",
)

#: Service category vocabulary shared with the AI JSON schema.
SERVICE_CATEGORIES = (
    "website_development",
    "ecommerce",
    "seo",
    "website_maintenance",
    "managed_it",
    "microsoft_365",
    "email_and_domains",
    "networking",
    "cybersecurity",
    "backups",
    "ai_automation",
    "workflow_automation",
    "crm",
    "integrations",
    "chatbots",
    "business_process_consulting",
    # Business phone systems: VoIP platforms, IVR, SIP trunking, and the
    # abuse that targets them. Added after a live Blue Collar post about a
    # telephony denial-of-service attack against a RingCentral IVR matched no
    # BruceTech service at all.
    "business_telephony",
    "none",
)

#: Values in service_categories that do not count as a real match.
EMPTY_SERVICE_VALUES = frozenset({"", "none", "n/a", "na", "null"})


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def normalize_enum(value: Any, *, default: str = "unknown") -> str:
    """Lowercase and underscore-normalise a model-supplied enum value."""
    if value is None:
        return default

    text = str(value).strip().lower()
    if not text:
        return default

    return text.replace(" ", "_").replace("-", "_")


def normalize_bool(value: Any) -> bool:
    """Coerce a model-supplied boolean-ish value to a real bool."""
    if isinstance(value, bool):
        return value

    if value is None:
        return False

    if isinstance(value, (int, float)):
        return value != 0

    return str(value).strip().lower() in {"true", "yes", "y", "1"}


def normalize_confidence(value: Any) -> float:
    """Coerce classification_confidence into the range 0.0 - 1.0."""
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0

    return max(0.0, min(confidence, 1.0))


def matched_service_categories(value: Any) -> list[str]:
    """Return the real service categories from a model-supplied list."""
    if value is None:
        return []

    if isinstance(value, str):
        raw: Iterable[Any] = [value]
    elif isinstance(value, Sequence):
        raw = value
    else:
        return []

    categories: list[str] = []
    for item in raw:
        category = normalize_enum(item, default="")
        if category and category not in EMPTY_SERVICE_VALUES:
            if category not in categories:
                categories.append(category)

    return categories


def parse_post_timestamp(value: Any) -> datetime | None:
    """
    Parse a Facebook/Airtable timestamp into an aware UTC datetime.

    Returns None when the value is missing or unparseable, which the caller
    treats as "age unknown" rather than "stale".
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(str(value).strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def post_age_in_days(
    value: Any,
    *,
    now: datetime | None = None,
) -> float | None:
    """Return the post age in days, or None when the timestamp is unusable."""
    parsed = parse_post_timestamp(value)
    if parsed is None:
        return None

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    return (reference - parsed).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class LeadDecision:
    """The deterministic outcome for a single post."""

    lead_score: int
    tier: str
    qualified: bool
    hard_rejected: bool
    hard_rejection_codes: list[str] = field(default_factory=list)
    manual_review: bool = False
    outreach_ready: bool = False
    rejection_reason: str = "None"
    suggested_dm: str = ""
    #: The short public Facebook comment that bridges into the DM. Subject to
    #: exactly the same outreach gate as suggested_dm: blank unless
    #: outreach_ready.
    suggested_comment: str = ""
    recommended_channel: str = CHANNEL_DO_NOT_CONTACT
    score_breakdown: dict[str, int] = field(default_factory=dict)

    @property
    def is_rejected(self) -> bool:
        return self.tier == TIER_REJECTED


# ---------------------------------------------------------------------------
# Hard rejections
# ---------------------------------------------------------------------------

def collect_hard_rejections(
    signals: dict[str, Any],
    *,
    age_days: float | None = None,
    max_post_age_days: int = DEFAULT_MAX_POST_AGE_DAYS,
    intent_type: str | None = None,
    post_text: Any = None,
    extra_codes: Sequence[str] = (),
    human_override: bool = False,
) -> list[str]:
    """
    Return every hard rejection code that applies to these signals.

    Order is stable so the primary reason is deterministic.

    ``intent_type`` and ``extra_codes`` carry the Python-side decisions --
    deterministic intent, author suppression, group exclusion, a human
    rejection -- that the model is never asked about.

    ``human_override`` means a person set Human Decision = "Approve" on the
    record. It lifts the codes in HUMAN_OVERRIDABLE_HARD_REJECTIONS -- the
    intent heuristics -- and nothing else. It is never derived from the
    Airtable ``Prequalification`` formula, which is machine generated and
    carries no human judgement. Everything in
    NON_OVERRIDABLE_HARD_REJECTIONS still applies, STALE_POST included, and
    outreach remains gated in evaluate_lead().
    """
    codes: list[str] = []

    def add(code: str) -> None:
        if code not in codes:
            codes.append(code)

    if normalize_bool(signals.get("personal_request")):
        add(REJECT_PERSONAL_REQUEST)

    if normalize_bool(signals.get("promotional_post")):
        add(REJECT_PROMOTIONAL_POST)

    if normalize_bool(signals.get("competitor_or_agency")):
        add(REJECT_COMPETITOR_OR_AGENCY)

    if normalize_bool(signals.get("free_only_request")):
        add(REJECT_FREE_ONLY_REQUEST)

    if normalize_bool(signals.get("provider_already_selected")):
        add(REJECT_PROVIDER_SELECTED)

    if normalize_enum(signals.get("resolved_status")) == "resolved":
        add(REJECT_ALREADY_RESOLVED)

    if normalize_enum(signals.get("spam_risk"), default="none") == "high":
        add(REJECT_SPAM_OR_AFFILIATE)

    if normalize_enum(
        signals.get("outreach_appropriateness"),
        default="appropriate",
    ) == "inappropriate":
        add(REJECT_INAPPROPRIATE_OUTREACH)

    if normalize_enum(signals.get("business_context"), default="none") == "none":
        add(REJECT_NO_BUSINESS_CONTEXT)

    if not matched_service_categories(signals.get("service_categories")):
        add(REJECT_NO_SERVICE_MATCH)

    # Codes the model raised directly.
    for raw_code in signals.get("disqualifier_codes") or []:
        code = str(raw_code).strip().upper().replace(" ", "_")
        if code in HARD_REJECTION_REASONS:
            add(code)

    # Staleness is computed in Python, never trusted from the model.
    if age_days is not None and age_days > max_post_age_days:
        add(REJECT_STALE_POST)

    # Deterministic intent. A post the classifier read as general business
    # advice or as unrelated chatter is never a BruceTech lead, however
    # generously the model scored it. This is the anti-solution-hopping rule.
    if intent_type is not None and intent_type not in intent.OUTREACH_INTENTS:
        if intent_type == intent.INTENT_TOOL_RESEARCH:
            # Research is not a rejection -- it is a human decision. Scoring
            # continues; outreach is blocked further down in evaluate_lead().
            pass
        else:
            add(intent_disqualifier_code(intent_type, post_text))

    for raw_code in extra_codes or ():
        code = str(raw_code).strip().upper().replace(" ", "_")
        if code in HARD_REJECTION_REASONS:
            add(code)

    # The override is applied last, as a filter over the finished list, so it
    # can only ever remove codes that are explicitly listed as overridable.
    if human_override:
        codes = apply_human_override(codes)

    return codes


def intent_disqualifier_code(
    intent_type: str,
    text: Any = None,
) -> str:
    """
    Pick the most specific rejection code for a non-serviceable intent.

    Financing and staffing questions get their own codes so the Airtable
    ``Disqualifiers`` column shows *why* a post was dropped rather than a
    catch-all.
    """
    if intent_type == intent.INTENT_GENERAL_ADVICE:
        subtype = intent.general_advice_subtype(text)
        if subtype == "FUNDING":
            return REJECT_FUNDING_REQUEST
        if subtype == "HIRING":
            return REJECT_HIRING_UNRELATED

    return REJECT_NO_BUYING_INTENT


def describe_hard_rejections(codes: Sequence[str]) -> str:
    """Build a human-readable rejection reason from hard rejection codes."""
    if not codes:
        return "None"

    return " ".join(
        HARD_REJECTION_REASONS.get(code, code) for code in codes
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_signals(signals: dict[str, Any]) -> dict[str, int]:
    """
    Convert structured AI signals into a per-component score breakdown.

    The caller sums these; the total is clamped to 0-100.
    """
    categories = matched_service_categories(signals.get("service_categories"))

    if categories:
        service_score = min(
            SERVICE_MATCH_BASE_SCORE
            + (len(categories) - 1) * SERVICE_MATCH_PER_EXTRA_CATEGORY,
            SERVICE_MATCH_MAX_SCORE,
        )
    else:
        service_score = 0

    breakdown: dict[str, int] = {
        "intent_strength": INTENT_STRENGTH_SCORES.get(
            normalize_enum(signals.get("intent_strength"), default="none"), 0
        ),
        "service_match": service_score,
        "problem_specificity": PROBLEM_SPECIFICITY_SCORES.get(
            normalize_enum(signals.get("problem_specificity"), default="none"),
            0,
        ),
        "business_context": BUSINESS_CONTEXT_SCORES.get(
            normalize_enum(signals.get("business_context"), default="none"), 0
        ),
        "purchase_signal": PURCHASE_SIGNAL_SCORES.get(
            normalize_enum(signals.get("purchase_signal"), default="none"), 0
        ),
        "urgency": URGENCY_SCORES.get(
            normalize_enum(signals.get("urgency"), default="none"), 0
        ),
        "business_impact": BUSINESS_IMPACT_SCORES.get(
            normalize_enum(signals.get("business_impact"), default="none"), 0
        ),
        "location": LOCATION_SCORES.get(
            normalize_enum(signals.get("location"), default="unknown"), 0
        ),
        "buyer_role": BUYER_ROLE_SCORES.get(
            normalize_enum(signals.get("buyer_role"), default="unknown"), 0
        ),
    }

    penalty = 0

    if normalize_enum(signals.get("resolved_status")) == "likely_resolved":
        penalty += PENALTY_LIKELY_RESOLVED

    spam_risk = normalize_enum(signals.get("spam_risk"), default="none")
    if spam_risk == "medium":
        penalty += PENALTY_SPAM_RISK_MEDIUM
    elif spam_risk == "low":
        penalty += PENALTY_SPAM_RISK_LOW

    if normalize_enum(
        signals.get("outreach_appropriateness"),
        default="appropriate",
    ) == "borderline":
        penalty += PENALTY_BORDERLINE_OUTREACH

    if normalize_confidence(
        signals.get("classification_confidence")
    ) < LOW_CONFIDENCE_CUTOFF:
        penalty += PENALTY_LOW_CONFIDENCE

    breakdown["penalties"] = -penalty
    return breakdown


def calculate_score(signals: dict[str, Any]) -> int:
    """Return the deterministic 0-100 lead score for these signals."""
    breakdown = score_signals(signals)
    return max(0, min(sum(breakdown.values()), 100))


def resolve_tier(
    score: int,
    *,
    hard_rejected: bool,
    threshold: int = DEFAULT_QUALIFICATION_THRESHOLD,
    manual_review_threshold: int = DEFAULT_MANUAL_REVIEW_THRESHOLD,
    hot_threshold: int = DEFAULT_HOT_THRESHOLD,
) -> str:
    """
    Map a score onto a tier.

    A hard rejection always wins, regardless of how high the score is.
    """
    if hard_rejected:
        return TIER_REJECTED

    if score >= hot_threshold:
        return TIER_HOT

    if score >= threshold:
        return TIER_QUALIFIED

    if score >= manual_review_threshold:
        return TIER_MANUAL_REVIEW

    return TIER_REJECTED


# ---------------------------------------------------------------------------
# Full evaluation
# ---------------------------------------------------------------------------

def evaluate_lead(
    signals: dict[str, Any],
    *,
    suggested_dm: str = "",
    suggested_comment: str = "",
    recommended_channel: str = CHANNEL_DO_NOT_CONTACT,
    post_time: Any = None,
    now: datetime | None = None,
    threshold: int = DEFAULT_QUALIFICATION_THRESHOLD,
    manual_review_threshold: int = DEFAULT_MANUAL_REVIEW_THRESHOLD,
    hot_threshold: int = DEFAULT_HOT_THRESHOLD,
    max_post_age_days: int = DEFAULT_MAX_POST_AGE_DAYS,
    intent_type: str | None = None,
    post_text: Any = None,
    min_outreach_confidence: float = DEFAULT_MIN_OUTREACH_CONFIDENCE,
    business_pain_min_score: int = DEFAULT_BUSINESS_PAIN_OUTREACH_SCORE,
    extra_disqualifiers: Sequence[str] = (),
    human_override: bool = False,
) -> LeadDecision:
    """
    Turn structured AI signals into the final, authoritative decision.

    The model never decides qualification. It supplies observations; this
    function applies the rules.

    ``intent_type`` is the deterministic classification from ``intent``. When
    supplied it can veto a lead outright (general advice, unrelated) and it
    gates outreach: only provider requests, implementation requests, and
    strong business pain may ever produce an automatic DM.

    ``human_override`` means a person set Human Decision = "Approve" on the
    record. It lifts the intent veto so their review is not undone by the
    heuristic they overrode. It does not lift outreach gating -- a human is
    already in the loop and will see the result -- and it does not lift
    anything in NON_OVERRIDABLE_HARD_REJECTIONS, STALE_POST included.

    It is never derived from the Airtable ``Prequalification`` formula. That
    field is machine generated from the Request, Service, and Promotion signal
    columns and carries no human judgement at all.
    """
    signals = signals or {}

    age_days = post_age_in_days(post_time, now=now) if post_time else None

    hard_codes = collect_hard_rejections(
        signals,
        age_days=age_days,
        max_post_age_days=max_post_age_days,
        intent_type=intent_type,
        post_text=post_text,
        extra_codes=extra_disqualifiers,
        human_override=human_override,
    )
    hard_rejected = bool(hard_codes)

    breakdown = score_signals(signals)
    score = max(0, min(sum(breakdown.values()), 100))

    tier = resolve_tier(
        score,
        hard_rejected=hard_rejected,
        threshold=threshold,
        manual_review_threshold=manual_review_threshold,
        hot_threshold=hot_threshold,
    )

    # Tool research is a human decision, never an automatic qualification.
    #
    # Someone comparing products has not asked for an implementer, however
    # well the model scores the post. A live Blue Collar record scored 76 and
    # was written to Airtable as Qualified while simultaneously being
    # do_not_contact with a blank DM, which is a self-contradictory row for a
    # salesperson to read.
    #
    # This caps the tier at Manual Review. It does not touch the score, the
    # weights, or any threshold: the model's analysis is preserved intact in
    # AI Output, and a hard rejection still overrides everything below.
    if (
        intent_type == intent.INTENT_TOOL_RESEARCH
        and not hard_rejected
        and tier in QUALIFYING_TIERS
    ):
        tier = TIER_MANUAL_REVIEW

    qualified = tier in QUALIFYING_TIERS
    manual_review = tier == TIER_MANUAL_REVIEW

    channel = normalize_enum(
        recommended_channel,
        default=CHANNEL_DO_NOT_CONTACT,
    )
    if channel not in OUTREACH_CHANNELS:
        channel = CHANNEL_DO_NOT_CONTACT

    # Every piece of outreach copy is sanitised on the way in, so no banned
    # character can reach Airtable even if the model ignores its instructions.
    dm = sanitize_outreach_copy(suggested_dm)
    comment = sanitize_outreach_copy(suggested_comment)

    if qualified:
        # Qualified means "strong commercial fit". Outreach Ready is a
        # stricter, separate question: is contacting this person right now
        # appropriate, evidenced, and wanted?
        #
        # A do_not_contact recommendation is never upgraded to an outreach
        # channel. If the model judged outreach inappropriate, that stands.
        confidence = normalize_confidence(
            signals.get("classification_confidence")
        )

        intent_allows_outreach = (
            intent_type is None or intent_type in intent.OUTREACH_INTENTS
        )

        # Business pain is an inference, not a request. Someone describing a
        # problem has not asked for a provider, so they need a higher score
        # before a cold DM is justified.
        pain_score_ok = (
            intent_type != intent.INTENT_BUSINESS_PAIN
            or score >= business_pain_min_score
        )

        outreach_ready = (
            bool(dm)
            and channel in OUTREACH_CHANNELS
            and confidence >= min_outreach_confidence
            and intent_allows_outreach
            and pain_score_ok
        )

        if not outreach_ready:
            # There is deliberately no generic fallback DM. A lead without a
            # personalised message is not outreach-ready.
            dm = ""
            comment = ""
            channel = CHANNEL_DO_NOT_CONTACT

        rejection_reason = "None"

    else:
        outreach_ready = False
        dm = ""
        comment = ""
        channel = CHANNEL_DO_NOT_CONTACT

        if hard_rejected:
            rejection_reason = describe_hard_rejections(hard_codes)
        elif manual_review:
            rejection_reason = (
                f"Manual review required: score {score} is between "
                f"{manual_review_threshold} and {threshold - 1}."
            )
        else:
            rejection_reason = (
                f"Score {score} is below the qualification threshold "
                f"of {threshold}."
            )

    return LeadDecision(
        lead_score=score,
        tier=tier,
        qualified=qualified,
        hard_rejected=hard_rejected,
        hard_rejection_codes=hard_codes,
        manual_review=manual_review,
        outreach_ready=outreach_ready,
        rejection_reason=rejection_reason,
        suggested_dm=dm,
        suggested_comment=comment,
        recommended_channel=channel,
        score_breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Deterministic prefilter
#
# Runs before any AI call so obviously unusable posts never cost a token, and
# produces the Prefilter Score used to order the AI queue.
#
# This replaces sole reliance on the Airtable Prequalification formula.
# Formula values calculate asynchronously and cannot be unit-tested, so Python
# repeats the check independently. The two must agree in spirit: a post needs
# a request signal AND a service signal AND no promotion signal.
# ---------------------------------------------------------------------------

MIN_PREFILTER_TEXT_LENGTH = 40

#: A post must reach this Prefilter Score to be worth an AI call.
DEFAULT_PREFILTER_PASS_SCORE = 30

# --- Prefilter Score components -------------------------------------------
#
# The score is a deterministic 0-100 *prioritisation* number. It is not the
# Lead Score: it is computed from the raw post alone, before the model has
# seen anything, and its only jobs are to gate the AI call and to sort the
# queue. Every component below is documented in README.md.

#: Base points by intent type. This dominates the score on purpose -- what
#: the author is asking for matters more than how they phrased it.
INTENT_BASE_SCORES: dict[str, int] = {
    intent.INTENT_PROVIDER_REQUEST: 35,
    intent.INTENT_IMPLEMENTATION_REQUEST: 32,
    intent.INTENT_BUSINESS_PAIN: 22,
    intent.INTENT_TOOL_RESEARCH: 10,
    intent.INTENT_GENERAL_ADVICE: 0,
    intent.INTENT_UNRELATED: 0,
}

#: Service match. A strong term is worth more than a corroborated weak one.
PREFILTER_STRONG_SERVICE_SCORE = 18
PREFILTER_SERVICE_PER_EXTRA_CATEGORY = 4
PREFILTER_SERVICE_MAX_SCORE = 26
PREFILTER_WEAK_SERVICE_SCORE = 8

#: Freshness. Fresh posts win; the author is still reading replies.
PREFILTER_AGE_SCORES: tuple[tuple[float, int], ...] = (
    (1.0, 18),   # under 24 hours
    (3.0, 12),   # 1-3 days
    (5.0, 6),    # 3-5 days
)
#: Older than the last band above but still within MAX_POST_AGE_DAYS.
PREFILTER_AGE_WITHIN_MAX_SCORE = 2
#: No usable timestamp: neither rewarded nor punished.
PREFILTER_AGE_UNKNOWN_SCORE = 4

#: Commercial context ("I own", "our clients", trade nouns).
PREFILTER_COMMERCIAL_SCORES: tuple[tuple[int, int], ...] = (
    (2, 10),     # two or more phrases
    (1, 6),      # one phrase
)

#: Length, as a rough proxy for how much the author explained.
PREFILTER_LENGTH_SCORES: tuple[tuple[int, int], ...] = (
    (400, 8),
    (180, 6),
    (80, 3),
)

#: Comment competition. A busy thread means the author already has options.
PREFILTER_COMMENT_SCORES: tuple[tuple[int, int], ...] = (
    (5, 8),      # 0-5 comments: high visibility
    (20, 4),     # 6-20: normal
    (50, 0),     # 21-50: competitive
)
#: More than the last band above.
PREFILTER_COMMENT_HEAVY_PENALTY = -6

#: Negative language penalties.
PREFILTER_PROMOTIONAL_PENALTY = 45
PREFILTER_JOB_SEEKER_PENALTY = 40
PREFILTER_RESOLVED_PENALTY = 35
PREFILTER_FREE_ONLY_PENALTY = 25
PREFILTER_NO_SERVICE_PENALTY = 20


def describe_match_basis(services: Any) -> str:
    """Name how a service match was established, for the audit trail."""
    if not getattr(services, "matched", False):
        return "none"

    if getattr(services, "strong_terms", None) or not (
        getattr(services, "operations_pain", None)
        or getattr(services, "adjacent_growth", None)
    ):
        return "named"

    if getattr(services, "adjacent_growth", None):
        return "adjacent"

    return "described"


@dataclass
class PrefilterResult:
    """Outcome of the deterministic, pre-AI screen."""

    passed: bool
    score: int
    intent_score: int
    reasons: list[str] = field(default_factory=list)
    intent_type: str = intent.INTENT_UNRELATED
    intent_label: str = "Unrelated"
    service_matched: bool = False
    service_categories: list[str] = field(default_factory=list)
    promotional: bool = False
    breakdown: dict[str, int] = field(default_factory=dict)
    #: Machine-readable reasons this post failed, using the same vocabulary as
    #: the post-AI hard rejections. ``reasons`` is prose for a human reading
    #: Airtable; this is what the pipeline branches on. Empty when ``passed``.
    rejection_codes: list[str] = field(default_factory=list)
    #: How the service match was established: "named" (the post used
    #: BruceTech vocabulary), "described" (it described an operational systems
    #: failure), "adjacent" (it asked for client acquisition BruceTech serves
    #: through the site, SEO, booking and CRM underneath), or "none".
    #:
    #: Recorded so an operator reading Airtable can tell a stated requirement
    #: from an inferred one. "adjacent" in particular means the model, not
    #: Python, decides whether real fit exists.
    match_basis: str = "none"

    @property
    def is_provider_request(self) -> bool:
        return self.intent_type == intent.INTENT_PROVIDER_REQUEST

    @property
    def allows_outreach(self) -> bool:
        return self.intent_type in intent.OUTREACH_INTENTS

    @property
    def is_stale(self) -> bool:
        """Older than the configured maximum age. Never overridable."""
        return REJECT_STALE_POST in self.rejection_codes

    @property
    def non_overridable_codes(self) -> list[str]:
        """
        The failures no human Approve may spend an AI call on.

        A post carrying any of these is rejected deterministically, before the
        OpenAI client is even constructed.
        """
        return [
            code
            for code in self.rejection_codes
            if code in NON_OVERRIDABLE_HARD_REJECTIONS
        ]

    @property
    def blocks_ai_call(self) -> bool:
        return bool(self.non_overridable_codes)


def _banded_score(
    value: float,
    bands: tuple[tuple[float, int], ...],
    default: int,
) -> int:
    """Return the score for the first band whose ceiling ``value`` is within."""
    for ceiling, points in bands:
        if value <= ceiling:
            return points
    return default


def _descending_banded_score(
    value: int,
    bands: tuple[tuple[int, int], ...],
    default: int = 0,
) -> int:
    """Return the score for the first band whose floor ``value`` reaches."""
    for floor, points in bands:
        if value >= floor:
            return points
    return default


def prefilter_post(
    text: Any,
    *,
    post_time: Any = None,
    now: datetime | None = None,
    max_post_age_days: int = DEFAULT_MAX_POST_AGE_DAYS,
    comment_count: Any = None,
    pass_score: int = DEFAULT_PREFILTER_PASS_SCORE,
    allow_tool_research: bool = True,
) -> PrefilterResult:
    """
    Cheaply screen a raw post before spending an AI call on it.

    A post passes only when **all** of these hold:

    1. It is long enough to evaluate.
    2. It is not older than ``max_post_age_days``.
    3. It credibly matches a BruceTech service (``intent.match_services``).
    4. It is not self-promotion, job seeking, or an already-resolved request.
    5. Its intent is one BruceTech can serve -- provider request,
       implementation request, business pain, or (when
       ``allow_tool_research``) tool research.
    6. Its Prefilter Score reaches ``pass_score``.

    Requiring 3 *and* 5 is what stops solution hopping. "I need a virtual
    assistant" is a real provider request with no BruceTech service behind it,
    and "how do I get financing" is a real business problem that BruceTech
    does not solve. Both fail here without costing a token.

    The authoritative decision is still made by ``evaluate_lead`` on the
    model's structured signals; this only decides who gets asked.
    """
    body = str(text or "").strip()
    reasons: list[str] = []
    codes: list[str] = []

    # Classify before the early returns. A rejected post still has its
    # assessment written to Airtable for a human to read, and reporting the
    # dataclass default there would label every short or stale post
    # "UNRELATED" regardless of what it actually said.
    intent_result = intent.classify_intent(body)
    services = intent.match_services(body)
    match_basis = describe_match_basis(services)

    def rejected(reason: str, code: str) -> PrefilterResult:
        return PrefilterResult(
            passed=False,
            score=0,
            intent_score=0,
            reasons=[reason],
            intent_type=intent_result.intent_type,
            intent_label=intent_result.label,
            service_matched=services.matched,
            service_categories=list(services.categories),
            rejection_codes=[code],
            match_basis=match_basis,
        )

    if len(body) < MIN_PREFILTER_TEXT_LENGTH:
        return rejected(
            "Post text is too short to evaluate.",
            REJECT_POST_TOO_SHORT,
        )

    # Age is checked here, before anything else can pass a post through, and
    # the code it emits is non-overridable. This is the gate that keeps an
    # expired lead from ever reaching the model.
    age_days = post_age_in_days(post_time, now=now) if post_time else None
    if age_days is not None and age_days > max_post_age_days:
        return rejected(
            f"Post is {age_days:.0f} days old, older than the "
            f"{max_post_age_days}-day maximum.",
            REJECT_STALE_POST,
        )

    negatives = intent.detect_negative_signals(body)
    commercial = intent.commercial_context_terms(body)

    intent_score = INTENT_BASE_SCORES.get(intent_result.intent_type, 0)

    if services.strong:
        service_score = min(
            PREFILTER_STRONG_SERVICE_SCORE
            + (len(services.categories) - 1)
            * PREFILTER_SERVICE_PER_EXTRA_CATEGORY,
            PREFILTER_SERVICE_MAX_SCORE,
        )
    elif services.matched:
        service_score = PREFILTER_WEAK_SERVICE_SCORE
    else:
        service_score = 0

    if age_days is None:
        age_score = PREFILTER_AGE_UNKNOWN_SCORE
    else:
        age_score = _banded_score(
            age_days, PREFILTER_AGE_SCORES, PREFILTER_AGE_WITHIN_MAX_SCORE
        )

    commercial_score = _descending_banded_score(
        len(commercial), PREFILTER_COMMERCIAL_SCORES
    )
    length_score = _descending_banded_score(
        len(body), PREFILTER_LENGTH_SCORES
    )

    try:
        comments = int(comment_count) if comment_count is not None else 0
    except (TypeError, ValueError):
        comments = 0

    comment_score = _banded_score(
        max(comments, 0),
        PREFILTER_COMMENT_SCORES,
        PREFILTER_COMMENT_HEAVY_PENALTY,
    )

    penalty = 0
    if negatives.promotional:
        penalty += PREFILTER_PROMOTIONAL_PENALTY
    if negatives.job_seeking:
        penalty += PREFILTER_JOB_SEEKER_PENALTY
    if negatives.resolved:
        penalty += PREFILTER_RESOLVED_PENALTY
    if negatives.free_only:
        penalty += PREFILTER_FREE_ONLY_PENALTY
    if not services.matched:
        penalty += PREFILTER_NO_SERVICE_PENALTY

    breakdown = {
        "intent": intent_score,
        "service_match": service_score,
        "freshness": age_score,
        "commercial_context": commercial_score,
        "length": length_score,
        "competition": comment_score,
        "penalties": -penalty,
    }

    score = max(0, min(sum(breakdown.values()), 100))

    # Reasons explain a rejection to a human reading Airtable, so they are
    # only worth recording when something is actually wrong.
    if not services.matched:
        reasons.append("No credible BruceTech service match.")
        codes.append(REJECT_NO_SERVICE_MATCH)

    if intent_result.intent_type == intent.INTENT_GENERAL_ADVICE:
        reasons.append(
            "General business advice question rather than a request "
            "BruceTech can serve."
        )
        codes.append(intent_disqualifier_code(intent_result.intent_type, body))
    elif intent_result.intent_type == intent.INTENT_UNRELATED:
        reasons.append("No request, problem, or buying intent detected.")
        codes.append(REJECT_NO_BUYING_INTENT)
    elif (
        intent_result.intent_type == intent.INTENT_TOOL_RESEARCH
        and not allow_tool_research
    ):
        reasons.append(
            "Tool research rather than a request for an implementer."
        )
        codes.append(REJECT_NO_BUYING_INTENT)

    if negatives.promotional:
        reasons.append(
            "Self-promotional language: "
            + ", ".join(sorted(set(negatives.promotional))[:5])
        )
        codes.append(REJECT_PROMOTIONAL_POST)
    if negatives.job_seeking:
        reasons.append("Job-seeking language.")
        codes.append(REJECT_JOB_SEEKER)
    if negatives.resolved:
        reasons.append("Request appears already resolved.")
        codes.append(REJECT_ALREADY_RESOLVED)
    if negatives.free_only:
        # Recorded for the human reading Airtable, but not a rejection code:
        # asking for free work is a scoring penalty here, not a pre-AI veto.
        reasons.append("Asking for free work only.")

    allowed_intents = set(intent.AI_ELIGIBLE_INTENTS)
    if not allow_tool_research:
        allowed_intents.discard(intent.INTENT_TOOL_RESEARCH)

    passed = (
        services.matched
        and intent_result.intent_type in allowed_intents
        and not negatives.promotional
        and not negatives.job_seeking
        and not negatives.resolved
        and score >= pass_score
    )

    if not passed and not reasons:
        reasons.append(
            f"Prefilter score {score} is below the {pass_score}-point "
            f"minimum."
        )

    return PrefilterResult(
        passed=passed,
        score=score,
        intent_score=intent_score,
        reasons=reasons,
        intent_type=intent_result.intent_type,
        intent_label=intent_result.label,
        service_matched=services.matched,
        service_categories=list(services.categories),
        promotional=bool(negatives.promotional),
        breakdown=breakdown,
        # A post that passed carries no rejection codes, by construction.
        rejection_codes=[] if passed else codes,
        match_basis=match_basis,
    )
