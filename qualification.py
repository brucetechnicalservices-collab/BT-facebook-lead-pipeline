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
DEFAULT_MAX_POST_AGE_DAYS = 45

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
}

#: disqualifier_codes the model may return that map onto a hard rejection.
KNOWN_DISQUALIFIER_CODES = tuple(HARD_REJECTION_REASONS.keys())

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
) -> list[str]:
    """
    Return every hard rejection code that applies to these signals.

    Order is stable so the primary reason is deterministic.
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

    return codes


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
    recommended_channel: str = CHANNEL_DO_NOT_CONTACT,
    post_time: Any = None,
    now: datetime | None = None,
    threshold: int = DEFAULT_QUALIFICATION_THRESHOLD,
    manual_review_threshold: int = DEFAULT_MANUAL_REVIEW_THRESHOLD,
    hot_threshold: int = DEFAULT_HOT_THRESHOLD,
    max_post_age_days: int = DEFAULT_MAX_POST_AGE_DAYS,
) -> LeadDecision:
    """
    Turn structured AI signals into the final, authoritative decision.

    The model never decides qualification. It supplies observations; this
    function applies the rules.
    """
    signals = signals or {}

    age_days = post_age_in_days(post_time, now=now) if post_time else None

    hard_codes = collect_hard_rejections(
        signals,
        age_days=age_days,
        max_post_age_days=max_post_age_days,
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

    qualified = tier in QUALIFYING_TIERS
    manual_review = tier == TIER_MANUAL_REVIEW

    channel = normalize_enum(
        recommended_channel,
        default=CHANNEL_DO_NOT_CONTACT,
    )
    if channel not in OUTREACH_CHANNELS:
        channel = CHANNEL_DO_NOT_CONTACT

    dm = str(suggested_dm or "").strip()

    if qualified:
        # A do_not_contact recommendation is never upgraded to an outreach
        # channel. If the model judged outreach inappropriate, that stands.
        outreach_ready = bool(dm) and channel in OUTREACH_CHANNELS

        if not outreach_ready:
            # There is deliberately no generic fallback DM. A lead without a
            # personalised message is not outreach-ready.
            dm = ""
            channel = CHANNEL_DO_NOT_CONTACT

        rejection_reason = "None"

    else:
        outreach_ready = False
        dm = ""
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
        recommended_channel=channel,
        score_breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Deterministic prefilter
#
# Runs before any AI call so obviously unusable posts never cost a token.
# This replaces sole reliance on the undocumented Airtable Prequalification
# formula.
# ---------------------------------------------------------------------------

MIN_PREFILTER_TEXT_LENGTH = 40

INTENT_KEYWORDS = (
    "recommend", "recommendation", "looking for", "need help", "need a",
    "need an", "anyone know", "any suggestions", "suggestions for",
    "who can", "can anyone", "help with", "advice on", "advice about",
    "hire", "quote", "pricing", "how much", "budget for", "looking to",
    "we need", "i need", "struggling with", "issues with", "problem with",
    "not working", "broken", "switch from", "migrate", "integrate",
    "automate", "set up", "setup", "build a", "redesign", "fix my",
    "fix our",
)

SERVICE_KEYWORDS = (
    "website", "web site", "wordpress", "shopify", "woocommerce",
    "landing page", "ecommerce", "e-commerce", "seo", "domain",
    "hosting", "booking system", "payment", "stripe",
    "it support", "managed it", "microsoft 365", "office 365", "outlook",
    "sharepoint", "onedrive", "teams", "email migration", "network",
    "wifi", "wi-fi", "vpn", "backup", "cybersecurity", "security",
    "crm", "airtable", "zapier", "make.com", "automation", "automate",
    "workflow", "integration", "api", "chatbot", "ai ",
    "artificial intelligence", "software", "pos ", "point of sale",
)

NEGATIVE_KEYWORDS = (
    "dm me", "pm me", "inbox me", "hire me", "i offer", "we offer",
    "my services", "our services", "for sale", "discount", "promo code",
    "limited time", "sign up now", "click the link", "affiliate",
    "looking for work", "looking for a job", "job seeker", "cv",
    "resume attached", "available for hire", "open to work",
    "free of charge", "for free", "no budget", "student project",
    "school project", "assignment", "homework", "thesis",
    "found someone", "sorted now", "issue resolved", "already fixed",
    "thanks everyone", "no longer needed",
)


@dataclass
class PrefilterResult:
    """Outcome of the deterministic, pre-AI screen."""

    passed: bool
    score: int
    intent_score: int
    reasons: list[str] = field(default_factory=list)


def prefilter_post(
    text: Any,
    *,
    post_time: Any = None,
    now: datetime | None = None,
    max_post_age_days: int = DEFAULT_MAX_POST_AGE_DAYS,
) -> PrefilterResult:
    """
    Cheaply screen a raw post before spending an AI call on it.

    Returns a score used both to gate the AI call and to prioritise the
    queue. This is heuristic on purpose -- the authoritative decision is
    still made by evaluate_lead() on the AI's structured signals.
    """
    body = str(text or "").strip()
    lowered = body.lower()
    reasons: list[str] = []

    if len(body) < MIN_PREFILTER_TEXT_LENGTH:
        return PrefilterResult(
            passed=False,
            score=0,
            intent_score=0,
            reasons=["Post text is too short to evaluate."],
        )

    age_days = post_age_in_days(post_time, now=now) if post_time else None
    if age_days is not None and age_days > max_post_age_days:
        return PrefilterResult(
            passed=False,
            score=0,
            intent_score=0,
            reasons=[
                f"Post is {age_days:.0f} days old, older than the "
                f"{max_post_age_days}-day maximum."
            ],
        )

    intent_hits = [word for word in INTENT_KEYWORDS if word in lowered]
    service_hits = [word for word in SERVICE_KEYWORDS if word in lowered]
    negative_hits = [word for word in NEGATIVE_KEYWORDS if word in lowered]

    intent_score = min(len(intent_hits) * 12, 48)
    service_score = min(len(service_hits) * 10, 40)
    length_score = 12 if len(body) >= 180 else 6
    penalty = min(len(negative_hits) * 20, 60)

    score = max(
        0,
        min(intent_score + service_score + length_score - penalty, 100),
    )

    if not intent_hits:
        reasons.append("No request or buying-intent language detected.")

    if not service_hits:
        reasons.append("No BruceTech service keywords detected.")

    if negative_hits:
        reasons.append(
            "Contains promotional, job-seeking, or resolved-request language: "
            + ", ".join(sorted(negative_hits)[:5])
        )

    # Require at least one intent signal AND one service signal, and a score
    # that survived the negative keywords.
    passed = bool(intent_hits) and bool(service_hits) and score >= 30

    return PrefilterResult(
        passed=passed,
        score=score,
        intent_score=intent_score,
        reasons=reasons,
    )
