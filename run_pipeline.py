"""
BruceTech Facebook lead pipeline.

Flow:

1.  Resolve exactly ONE Apify run, log it to Facebook Post Scraper Runs, and
    read that run's own dataset.
2.  Deduplicate against Airtable using post ID, canonical URL, and text
    fingerprint, then import only genuinely new posts -- each linked back to
    the run that produced it.
3.  Classify intent and match BruceTech services deterministically, then
    screen the backlog with the Python prefilter.
4.  Ask the AI to extract *structured signals only*, best leads first.
5.  Score, tier, and qualify in Python. The AI never decides qualification.
6.  Refresh Facebook Group Performance so lead quality is measurable per
    group.

Environment variables are read lazily so this module can be imported by the
test suite without any credentials present.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence
from urllib.parse import quote

import requests

import ai_retry
import apify_runs
import group_performance
import intent
import scraper_runs
from normalization import (
    build_seen_keys,
    deduplicate_posts,
    normalize_facebook_url,
)
from qualification import (
    DEFAULT_BUSINESS_PAIN_OUTREACH_SCORE,
    DEFAULT_HOT_THRESHOLD,
    DEFAULT_MANUAL_REVIEW_THRESHOLD,
    DEFAULT_MAX_POST_AGE_DAYS,
    DEFAULT_MIN_OUTREACH_CONFIDENCE,
    DEFAULT_PREFILTER_PASS_SCORE,
    DEFAULT_QUALIFICATION_THRESHOLD,
    HARD_REJECTION_REASONS,
    KNOWN_DISQUALIFIER_CODES,
    NON_OVERRIDABLE_HARD_REJECTIONS,
    REJECT_DISALLOWED_GROUP,
    REJECT_HUMAN_REJECTED,
    REJECT_STALE_POST,
    REJECT_SUPPRESSED_AUTHOR,
    SERVICE_CATEGORIES,
    TIER_MANUAL_REVIEW,
    TIER_REJECTED,
    LeadDecision,
    PrefilterResult,
    evaluate_lead,
    parse_post_timestamp,
    prefilter_post,
)


# ---------------------------------------------------------------------------
# Environment configuration
#
# Nothing here raises at import time. Secrets are validated by
# require_env() at the point of use so tests can import this module.
# ---------------------------------------------------------------------------

def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default

    try:
        return int(str(raw).strip())
    except ValueError:
        print(
            f"Invalid integer for {name}={raw!r}; using {default}.",
            flush=True,
        )
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default

    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default

    try:
        return float(str(raw).strip())
    except ValueError:
        print(
            f"Invalid number for {name}={raw!r}; using {default}.",
            flush=True,
        )
        return default


def env_csv(name: str) -> frozenset[str]:
    """Read a comma-separated env var into a lowercased set."""
    raw = os.getenv(name, "")

    return frozenset(
        part.strip().lower() for part in str(raw).split(",") if part.strip()
    )


def require_env(name: str) -> str:
    """Return a required environment variable or fail with a clear message."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable {name} is not set."
        )
    return value


OPENAI_MODEL = env_str("OPENAI_MODEL", "gpt-5-mini")

#: How many OpenAI calls one run may make. This is a *spend* limit and
#: nothing else: it caps calls, never how many records are evaluated.
AI_BATCH_LIMIT = env_int("AI_BATCH_LIMIT", 20)

#: How many historical backlog records to pull in for deterministic
#: evaluation each run.
#:
#: Deterministic evaluation is regex over post text -- effectively free. This
#: limit exists only to bound the Airtable read against a table that now holds
#: thousands of records, and is deliberately independent of AI_BATCH_LIMIT.
#:
#: Records imported by the current run are NOT governed by this limit. They
#: are fetched by record ID and evaluated in full, however many there are --
#: see fetch_ai_queue. Before 2026-08-19 the queue window was
#: ``AI_BATCH_LIMIT * 5``, so a live run with AI_BATCH_LIMIT=5 evaluated 25 of
#: its 50 freshly imported posts and silently ignored the rest.
PREFILTER_SCAN_LIMIT = env_int("PREFILTER_SCAN_LIMIT", 200)

#: Minimum score for Qualified. Raised from 55 to 65.
QUALIFICATION_THRESHOLD = env_int(
    "QUALIFICATION_THRESHOLD", DEFAULT_QUALIFICATION_THRESHOLD
)
MANUAL_REVIEW_THRESHOLD = env_int(
    "MANUAL_REVIEW_THRESHOLD", DEFAULT_MANUAL_REVIEW_THRESHOLD
)
HOT_LEAD_THRESHOLD = env_int("HOT_LEAD_THRESHOLD", DEFAULT_HOT_THRESHOLD)
MAX_POST_AGE_DAYS = env_int("MAX_POST_AGE_DAYS", DEFAULT_MAX_POST_AGE_DAYS)

AIRTABLE_FORMULA_WAIT_SECONDS = env_int("AIRTABLE_FORMULA_WAIT_SECONDS", 15)
OPENAI_MAX_OUTPUT_TOKENS = env_int("OPENAI_MAX_OUTPUT_TOKENS", 2500)
OPENAI_MAX_ATTEMPTS = env_int("OPENAI_MAX_ATTEMPTS", 3)
OPENAI_RETRY_DELAY_SECONDS = env_int("OPENAI_RETRY_DELAY_SECONDS", 5)
MAX_POST_CHARS = env_int("MAX_POST_CHARS", 8000)

#: When true, start a fresh Apify task run and use that exact run's dataset.
#: RUN_APIFY_TASK is the documented name; APIFY_START_NEW_RUN is kept as an
#: alias so existing workflow configuration keeps working.
APIFY_START_NEW_RUN = env_bool(
    "RUN_APIFY_TASK", env_bool("APIFY_START_NEW_RUN", False)
)

#: Pin the pipeline to one exact Apify run. Overrides RUN_APIFY_TASK. Use it
#: to re-import a specific scrape or to reproduce a bad import.
APIFY_RUN_ID = env_str("APIFY_RUN_ID", "")

APIFY_RUN_TIMEOUT_SECONDS = env_int("APIFY_RUN_TIMEOUT_SECONDS", 900)
APIFY_RUN_POLL_SECONDS = env_int(
    "APIFY_POLL_INTERVAL_SECONDS", env_int("APIFY_RUN_POLL_SECONDS", 15)
)

#: Write one Facebook Post Scraper Runs record per Apify run, and link every
#: imported Raw Signal to it.
LOG_SCRAPER_RUNS = env_bool("LOG_SCRAPER_RUNS", True)

#: Recompute Facebook Group Performance counts at the end of each run.
UPDATE_GROUP_PERFORMANCE = env_bool("UPDATE_GROUP_PERFORMANCE", True)

#: Stamped on every record this release evaluates, so legacy and new
#: qualification results can be compared. Historical records are never
#: rewritten -- a record only gets a version when the queue processes it.
QUALIFICATION_VERSION = env_str("QUALIFICATION_VERSION", "facebook-v2")

#: Re-queue records whose last AI attempt failed with a transient error.
RETRY_AI_ERRORS = env_bool("RETRY_AI_ERRORS", True)

#: Total AI attempts allowed per record ACROSS runs. Prevents a permanently
#: broken record from consuming the batch every night.
AI_ERROR_MAX_ATTEMPTS = env_int("AI_ERROR_MAX_ATTEMPTS", 3)

#: Minimum Prefilter Score before a post is worth an AI call.
PREFILTER_PASS_SCORE = env_int(
    "PREFILTER_PASS_SCORE", DEFAULT_PREFILTER_PASS_SCORE
)

#: Send tool-research posts to the AI. They can reach Manual Review but never
#: automatic outreach. Set false to stop spending tokens on them entirely.
ALLOW_TOOL_RESEARCH_TO_AI = env_bool("ALLOW_TOOL_RESEARCH_TO_AI", True)

#: Minimum classification_confidence before automatic outreach is allowed.
MIN_OUTREACH_CONFIDENCE = env_float(
    "MIN_OUTREACH_CONFIDENCE", DEFAULT_MIN_OUTREACH_CONFIDENCE
)

#: Business-pain leads must clear this score before automatic outreach.
BUSINESS_PAIN_OUTREACH_MIN_SCORE = env_int(
    "BUSINESS_PAIN_OUTREACH_MIN_SCORE", DEFAULT_BUSINESS_PAIN_OUTREACH_SCORE
)

#: Authors and groups excluded from outreach entirely. Comma-separated.
SUPPRESSED_AUTHORS = env_csv("SUPPRESSED_AUTHORS")
DISALLOWED_GROUPS = env_csv("DISALLOWED_GROUPS")

#: The Airtable Prequalification formula is machine generated: it derives
#: "Send to AI" from the Request, Service, and Promotion signal columns and
#: knows nothing about post age, human review, or intent. It is an
#: informational eligibility hint only -- Python is authoritative for pre-AI
#: eligibility, and no code path may treat this field as human approval.
#: Set this to true to additionally *narrow* the queue to records the formula
#: already likes; it can never widen what Python allows through.
REQUIRE_AIRTABLE_PREQUALIFICATION = env_bool(
    "REQUIRE_AIRTABLE_PREQUALIFICATION", False
)

#: Skip the AI call entirely for posts the deterministic prefilter rejects.
ENFORCE_PYTHON_PREFILTER = env_bool("ENFORCE_PYTHON_PREFILTER", True)

#: Read from Apify and Airtable and call the AI as normal, but make no
#: Airtable writes. Use this to see how the new rules score real records
#: before letting the pipeline modify the base.
DRY_RUN = env_bool("DRY_RUN", False)


# ---------------------------------------------------------------------------
# Airtable field names
# ---------------------------------------------------------------------------

FIELD_URL = "Url"
FIELD_FACEBOOK_URL = "Facebook url"
FIELD_TIME = "Time"
FIELD_USER_ID = "User ID"
FIELD_USER_NAME = "User name"
FIELD_TEXT = "Text"
FIELD_GROUP_TITLE = "Group title"
FIELD_INPUT_URL = "Input Url"
FIELD_LIKES = "Likes count"
FIELD_COMMENTS = "Comments count"
FIELD_SHARES = "Shares count"

FIELD_PREQUALIFICATION = "Prequalification"
FIELD_AI_STATUS = "AI Status"
FIELD_QUALIFIED = "Qualified"
FIELD_LEAD_SCORE = "Lead Score"
FIELD_SERVICE_MATCH = "Service Match"
FIELD_LEAD_SUMMARY = "Lead Summary"
FIELD_REJECTION_REASON = "Rejection Reason"
FIELD_SUGGESTED_DM = "Suggested DM"
FIELD_RECOMMENDED_CHANNEL = "Recommended channel"
FIELD_EVIDENCE = "Evidence"

FIELD_AI_OUTPUT = "AI Output"

# Tiering fields, added by the deterministic-qualification release.
FIELD_LEAD_TIER = "Lead Tier"
FIELD_MANUAL_REVIEW = "Manual Review"
FIELD_OUTREACH_READY = "Outreach Ready"
FIELD_DISQUALIFIERS = "Disqualifiers"
FIELD_PREFILTER_SCORE = "Prefilter Score"

# Run attribution and feedback-loop fields, added by this release.
FIELD_QUALIFICATION_VERSION = "Qualification Version"
FIELD_APIFY_RUN_ID = "Apify Run ID"
FIELD_SCRAPER_RUN = "Scraper Run"

# Human-owned fields. READ ONLY for this pipeline -- a classification run must
# never overwrite a person's decision or a recorded sales outcome.
FIELD_HUMAN_DECISION = "Human Decision"
FIELD_OUTREACH_STATUS = "Outreach Status"
FIELD_LAST_CONTACTED = "Last Contacted"
FIELD_CONTACTED_LEGACY = "Contacted"

# Airtable formula fields. NEVER written -- Airtable computes them, and a
# write attempt is an API error.
FIELD_REQUEST_SIGNAL = "Request Signal"
FIELD_SERVICE_SIGNAL = "Service Signal"
FIELD_PROMOTION_SIGNAL = "Promotion Signal"
FIELD_PROVIDER_INTENT_SIGNAL = "Provider Intent Signal"
FIELD_RESEARCH_INTENT_SIGNAL = "Research Intent Signal"
FIELD_INTENT_TYPE = "Intent Type"

#: Guard rail. Every Airtable payload is checked against this set before it is
#: sent, so a formula field or a human-owned field can never be written by
#: accident. Airtable rejects formula writes anyway; the human-owned fields it
#: would happily overwrite, which is the dangerous case.
READ_ONLY_FIELDS = frozenset(
    {
        FIELD_REQUEST_SIGNAL,
        FIELD_SERVICE_SIGNAL,
        FIELD_PROMOTION_SIGNAL,
        FIELD_PROVIDER_INTENT_SIGNAL,
        FIELD_RESEARCH_INTENT_SIGNAL,
        FIELD_INTENT_TYPE,
        FIELD_PREQUALIFICATION,
        FIELD_HUMAN_DECISION,
        FIELD_OUTREACH_STATUS,
        FIELD_LAST_CONTACTED,
        FIELD_CONTACTED_LEGACY,
    }
)

#: Facebook Post Scraper Runs has no formula or human-owned columns, and
#: shares no column names with Raw Signals. Declared explicitly so the guard
#: is a deliberate choice per table rather than an inherited default.
SCRAPER_RUNS_PROTECTED_FIELDS: frozenset[str] = frozenset()

EXTENDED_FIELDS = (
    FIELD_LEAD_TIER,
    FIELD_MANUAL_REVIEW,
    FIELD_OUTREACH_READY,
    FIELD_DISQUALIFIERS,
    FIELD_PREFILTER_SCORE,
    FIELD_QUALIFICATION_VERSION,
    FIELD_APIFY_RUN_ID,
    FIELD_SCRAPER_RUN,
)

#: Every Raw Signals field the pipeline writes.
#:
#: The preflight validates this whole set, not a subset. A field missing from
#: here but present in a payload fails mid-batch with UNKNOWN_FIELD_NAME
#: instead of failing clearly before anything is modified -- and AI Output in
#: particular now carries the cross-run retry counter, so losing it silently
#: breaks error recovery.
REQUIRED_RAW_SIGNAL_FIELDS = (
    # Written on import.
    FIELD_URL,
    FIELD_FACEBOOK_URL,
    FIELD_TIME,
    FIELD_USER_ID,
    FIELD_USER_NAME,
    FIELD_TEXT,
    FIELD_GROUP_TITLE,
    FIELD_INPUT_URL,
    FIELD_LIKES,
    FIELD_COMMENTS,
    FIELD_SHARES,
    FIELD_APIFY_RUN_ID,
    FIELD_SCRAPER_RUN,
    # Written after qualification.
    FIELD_AI_STATUS,
    FIELD_QUALIFIED,
    FIELD_LEAD_SCORE,
    FIELD_SERVICE_MATCH,
    FIELD_LEAD_SUMMARY,
    FIELD_REJECTION_REASON,
    FIELD_SUGGESTED_DM,
    FIELD_RECOMMENDED_CHANNEL,
    FIELD_EVIDENCE,
    FIELD_AI_OUTPUT,
    FIELD_LEAD_TIER,
    FIELD_MANUAL_REVIEW,
    FIELD_OUTREACH_READY,
    FIELD_DISQUALIFIERS,
    FIELD_PREFILTER_SCORE,
    FIELD_QUALIFICATION_VERSION,
)

#: Airtable table names. Only AIRTABLE_TABLE_NAME is a required secret; the
#: other two default to their live names.
SCRAPER_RUNS_TABLE = env_str(
    "AIRTABLE_SCRAPER_RUNS_TABLE", "Facebook Post Scraper Runs"
)
GROUP_PERFORMANCE_TABLE = env_str(
    "AIRTABLE_GROUP_PERFORMANCE_TABLE", "Facebook Group Performance"
)


def airtable_table_url(table: str) -> str:
    """Build the Airtable REST URL for one table in the configured base."""
    base_id = require_env("AIRTABLE_BASE_ID")
    return (
        "https://api.airtable.com/v0/"
        f"{base_id}/{quote(table, safe='')}"
    )


def airtable_url() -> str:
    """The Facebook Raw Signals table."""
    # Base ID is resolved first so a missing base reports the base, not the
    # table, as the thing that is not configured.
    require_env("AIRTABLE_BASE_ID")
    return airtable_table_url(require_env("AIRTABLE_TABLE_NAME"))


def strip_read_only_fields(
    fields: dict[str, Any],
    protected: frozenset[str] = READ_ONLY_FIELDS,
) -> dict[str, Any]:
    """
    Remove fields the pipeline must never write.

    Airtable rejects formula writes with an error, but it would happily
    overwrite Human Decision or Outreach Status. This is the last line of
    defence before any PATCH or POST leaves the process.

    ``protected`` is per-table and must be passed explicitly for anything
    other than Facebook Raw Signals. Column names are not unique across the
    base: "Contacted" is a read-only checkbox on Raw Signals and a computed
    count on Facebook Group Performance, so applying the Raw Signals set to
    the group table would silently drop a metric the pipeline owns.
    """
    removed = sorted(set(fields) & protected)

    if removed:
        print(
            f"Refusing to write read-only Airtable fields: "
            f"{', '.join(removed)}. This is a bug; the fields were dropped.",
            file=sys.stderr,
            flush=True,
        )

    return {
        key: value for key, value in fields.items() if key not in protected
    }


def airtable_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {require_env('AIRTABLE_TOKEN')}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# OpenAI client (lazy)
#
# Created on first use so importing this module -- and running the
# qualification tests -- never requires an API key or an active client.
# ---------------------------------------------------------------------------

_OPENAI_CLIENT: Any = None


def get_openai_client() -> Any:
    """Return a lazily constructed OpenAI client."""
    global _OPENAI_CLIENT

    if _OPENAI_CLIENT is None:
        from openai import OpenAI

        _OPENAI_CLIENT = OpenAI(api_key=require_env("OPENAI_API_KEY"))

    return _OPENAI_CLIENT


def reset_openai_client() -> None:
    """Drop the cached client. Used by tests."""
    global _OPENAI_CLIENT
    _OPENAI_CLIENT = None


# ---------------------------------------------------------------------------
# OpenAI instructions
#
# The model is a *signal extractor*. It does not score, qualify, or decide
# outreach; run_pipeline applies those rules deterministically.
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTIONS = """
You are BruceTech's Facebook lead signal extractor.

BruceTech is a Toronto-based web development, managed IT, Microsoft 365,
cybersecurity, AI consulting, and business automation company.

BruceTech services include:

Website and e-commerce:
- Website design, development, and redesign
- WordPress, Shopify, and WooCommerce support
- Landing pages, booking systems, payments and Stripe
- E-commerce, SEO, website maintenance
- Business email and domain setup

Managed IT:
- Managed IT support, Microsoft 365, Outlook and business email
- Email migrations, SharePoint, OneDrive, Teams
- Device support, network and Wi-Fi, VPN, backups
- Cybersecurity, user onboarding and permissions

AI, workflow, and consulting:
- AI automation consultation and readiness assessment
- Workflow automation, business-process consulting and process mapping
- CRM implementation, Airtable, Make, and Zapier workflows
- API and system integrations
- Lead qualification and customer follow-up automation
- Reporting, data-entry, document, and order-processing automation
- AI chatbots, receptionists, and internal assistants

YOUR ROLE

You do NOT decide whether a lead is qualified. You do NOT produce a score.
You observe the post and report structured signals. A separate deterministic
system applies the scoring and qualification rules.

Analyze only the supplied public Facebook post and metadata. Use ONLY
evidence contained in the post and context supplied. Never invent:

- business names or business details
- urgency
- budget
- location
- service requirements
- website, system, or technical problems

If the post does not support a signal, report the lowest or "unknown" value.
Do not guess upward. Under-reporting is safer than over-reporting.

DO NOT SOLUTION-HOP

Do NOT report a service match merely because BruceTech could theoretically
provide an alternative solution to the author's situation. The need the
author expressed must directly align with a BruceTech service, or clearly
describe implementation work or operational pain that BruceTech solves.

  "I need a virtual assistant."
    -> NOT a BruceTech need. Do not reason "BruceTech could automate this
       instead". service_categories = [] unless they explicitly ask about
       automation or AI.

  "I need business financing for another truck."
    -> NOT a BruceTech need. Do not reason "BruceTech could improve their
       systems". service_categories = [].

  "Who has used a good CRM?"
    -> Research, not a request for an implementer. intent_strength is at
       most "weak" and purchase_signal at most "researching".

  "Looking for someone to set up GoHighLevel with SMS and email sequences."
    -> A genuine provider request. Report it at full strength.

A Python classifier has already decided the post's intent type and whether it
matches a BruceTech service. Contradicting the post to be helpful does not
create a lead; it only wastes a human's time.

SIGNAL GUIDANCE

intent_strength - how clearly the author is asking for help or a provider.
  strong  = explicitly asking for a recommendation, provider, or quote
  moderate= describing a problem they clearly want solved
  weak    = passing mention of a problem
  none    = no request or problem

business_context - is there a credible business or organization?
  Use "none" for a private individual with no business context.

buyer_role - the author's apparent authority to buy.

service_categories - every BruceTech service area the post plausibly needs.
  Return an empty list (or ["none"]) when nothing matches.

problem_specificity - how concretely the problem is described.

purchase_signal - how close the author appears to be to buying.

urgency and business_impact - only when the post supports them.

location - Toronto/GTA, Ontario, Canada, other, or unknown.

resolved_status - "resolved" when the author says the problem is solved or
  thanks people for answers; "likely_resolved" when it reads that way but is
  not explicit.

provider_already_selected - the author names or indicates a chosen provider.

personal_request - a consumer/household need rather than a business need.

free_only_request - explicitly asking for free work only.

promotional_post - the author is advertising their own products or services.

competitor_or_agency - the author is an agency, freelancer, or consultant
  promoting themselves.

spam_risk - affiliate links, engagement bait, or scam patterns.

outreach_appropriateness - would a respectful cold DM be appropriate?
  Use "inappropriate" for grief, illness, crisis, political, or otherwise
  sensitive posts.

classification_confidence - 0.0 to 1.0, how confident you are overall.

disqualifier_codes - report every code that applies, from this list:
""" + "\n".join(f"  - {code}" for code in KNOWN_DISQUALIFIER_CODES) + """

Report JOB_SEEKER for people seeking work or offering labour, and
STUDENT_OR_EDUCATIONAL for students, coursework, or purely academic
questions. These have no dedicated boolean field.

WRITTEN OUTPUT

lead_summary - one or two factual sentences about what the author needs.

evidence - short quotes or specifics from the post supporting your signals.

service_match - a short human-readable label, or "None".

suggested_dm - only write one when the post genuinely supports a specific,
  personalised message:
  - under 80 words, natural, no marketing voice
  - reference only details actually present in the post
  - include brucetech.ca naturally
  - end with one simple question
  - never mention scraping, monitoring, tracking, or AI analysis
  - never claim BruceTech reviewed their website or systems
  If you cannot write a genuinely specific message, return an empty string.
  Never write a generic template. A blank DM is correct and expected.

recommended_channel - do_not_contact whenever outreach would be unwelcome,
  intrusive, or unsupported by the post. This is respected as-is and is
  never upgraded, so use it whenever you have doubts.

Always return every field.
""".strip()


def _enum(values: tuple[str, ...]) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


LEAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent_strength": _enum(("none", "weak", "moderate", "strong")),
        "business_context": _enum(
            (
                "none",
                "unclear",
                "individual_or_sole_trader",
                "small_business",
                "established_business",
            )
        ),
        "buyer_role": _enum(
            (
                "unknown",
                "other",
                "employee",
                "manager",
                "owner_or_decision_maker",
            )
        ),
        "service_categories": {
            "type": "array",
            "items": _enum(SERVICE_CATEGORIES),
        },
        "problem_specificity": _enum(
            ("none", "vague", "specific", "detailed")
        ),
        "purchase_signal": _enum(
            ("none", "researching", "comparing_options", "ready_to_buy")
        ),
        "urgency": _enum(("none", "low", "medium", "high")),
        "business_impact": _enum(("none", "low", "medium", "high")),
        "location": _enum(
            ("unknown", "other", "canada", "ontario", "toronto_gta")
        ),
        "resolved_status": _enum(
            ("unresolved", "likely_resolved", "resolved")
        ),
        "provider_already_selected": {"type": "boolean"},
        "personal_request": {"type": "boolean"},
        "free_only_request": {"type": "boolean"},
        "promotional_post": {"type": "boolean"},
        "competitor_or_agency": {"type": "boolean"},
        "spam_risk": _enum(("none", "low", "medium", "high")),
        "outreach_appropriateness": _enum(
            ("appropriate", "borderline", "inappropriate")
        ),
        "classification_confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "disqualifier_codes": {
            "type": "array",
            "items": _enum(KNOWN_DISQUALIFIER_CODES),
        },
        "service_match": {"type": "string"},
        "lead_summary": {"type": "string"},
        "evidence": {"type": "string"},
        "suggested_dm": {"type": "string"},
        "recommended_channel": _enum(
            ("direct_message", "public_reply_then_dm", "do_not_contact")
        ),
    },
    "required": [
        "intent_strength",
        "business_context",
        "buyer_role",
        "service_categories",
        "problem_specificity",
        "purchase_signal",
        "urgency",
        "business_impact",
        "location",
        "resolved_status",
        "provider_already_selected",
        "personal_request",
        "free_only_request",
        "promotional_post",
        "competitor_or_agency",
        "spam_risk",
        "outreach_appropriateness",
        "classification_confidence",
        "disqualifier_codes",
        "service_match",
        "lead_summary",
        "evidence",
        "suggested_dm",
        "recommended_channel",
    ],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def request_with_retry(
    method: str,
    url: str,
    *,
    max_attempts: int = 5,
    **kwargs: Any,
) -> requests.Response:
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.request(method, url, timeout=120, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == max_attempts:
                break

            wait_seconds = min(2 ** attempt, 30)
            print(
                f"Network error: {exc}. Retrying in {wait_seconds}s...",
                flush=True,
            )
            time.sleep(wait_seconds)
            continue

        if response.status_code < 400:
            return response

        if response.status_code in {429, 500, 502, 503, 504}:
            if attempt == max_attempts:
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")
            wait_seconds = (
                int(retry_after)
                if retry_after and retry_after.isdigit()
                else min(2 ** attempt, 30)
            )
            print(
                f"Request failed with {response.status_code}. "
                f"Retrying in {wait_seconds}s...",
                flush=True,
            )
            time.sleep(wait_seconds)
            continue

        raise RuntimeError(
            f"Request failed: {response.status_code} {response.text}"
        )

    raise RuntimeError(
        f"Request failed after {max_attempts} attempts: {url}. "
        f"Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# Apify
#
# Run resolution and dataset retrieval live in apify_runs.py. This section
# only supplies credentials and configuration.
#
# The old implementation read `/actor-tasks/{id}/runs/last/dataset/items`,
# which returns items with no run identity attached. If a manual Apify run
# finished between the scheduled scrape and this read, the pipeline imported
# that run's posts while believing it had read its own -- and recorded no run
# ID either way. Every path now resolves one exact run object first.
# ---------------------------------------------------------------------------

APIFY_DATASET_FIELDS = apify_runs.DATASET_FIELDS


def resolve_apify_run(
    *,
    start_new_run: bool | None = None,
) -> apify_runs.ApifyRun:
    """
    Resolve the single Apify run this execution will import from.

    ``start_new_run`` overrides the configured mode. main() passes False in
    dry-run mode so a validation pass never starts a paid actor run.
    """
    return apify_runs.resolve_run(
        request_with_retry,
        token=require_env("APIFY_TOKEN"),
        task_id=require_env("APIFY_TASK_ID"),
        start_new_run=(
            APIFY_START_NEW_RUN if start_new_run is None else start_new_run
        ),
        run_id=APIFY_RUN_ID,
        timeout_seconds=APIFY_RUN_TIMEOUT_SECONDS,
        poll_seconds=APIFY_RUN_POLL_SECONDS,
    )


def collect_apify_posts(
    run: apify_runs.ApifyRun,
) -> list[dict[str, Any]]:
    """Read the posts belonging to one exact resolved run."""
    return apify_runs.collect_posts(
        request_with_retry,
        token=require_env("APIFY_TOKEN"),
        run=run,
    )


def fetch_apify_run_input(run: apify_runs.ApifyRun) -> Any:
    """Read the exact input the run was started with, if Apify stored one."""
    return apify_runs.fetch_run_input(
        request_with_retry,
        token=require_env("APIFY_TOKEN"),
        run=run,
    )


# ---------------------------------------------------------------------------
# Airtable reads and deduplication
# ---------------------------------------------------------------------------

def list_table_records(
    url: str,
    *,
    formula: str | None = None,
    fields: list[str] | None = None,
    max_records: int | None = None,
    sort_field: str | None = None,
    sort_direction: str = "desc",
) -> list[dict[str, Any]]:
    """
    Page through the records of any table in the base.

    ``sort_field`` matters whenever ``max_records`` is set: without a sort,
    Airtable returns records in the table's own order, so truncating gives an
    arbitrary slice rather than the most relevant one.
    """
    records: list[dict[str, Any]] = []
    offset: str | None = None
    headers = airtable_headers()

    while True:
        params: list[tuple[str, str | int]] = [("pageSize", 100)]

        if formula:
            params.append(("filterByFormula", formula))

        if sort_field:
            params.append(("sort[0][field]", sort_field))
            params.append(("sort[0][direction]", sort_direction))

        if fields:
            for field_name in fields:
                params.append(("fields[]", field_name))

        if offset:
            params.append(("offset", offset))

        response = request_with_retry(
            "GET", url, headers=headers, params=params
        )

        data = response.json()
        records.extend(data.get("records", []))

        if max_records is not None and len(records) >= max_records:
            return records[:max_records]

        offset = data.get("offset")
        if not offset:
            break

        time.sleep(0.25)

    return records


def list_airtable_records(**kwargs: Any) -> list[dict[str, Any]]:
    """Page through the Facebook Raw Signals table."""
    return list_table_records(airtable_url(), **kwargs)


def list_scraper_run_records(**kwargs: Any) -> list[dict[str, Any]]:
    """Page through the Facebook Post Scraper Runs table."""
    return list_table_records(
        airtable_table_url(SCRAPER_RUNS_TABLE), **kwargs
    )


def list_group_performance_records(**kwargs: Any) -> list[dict[str, Any]]:
    """Page through the Facebook Group Performance table."""
    return list_table_records(
        airtable_table_url(GROUP_PERFORMANCE_TABLE), **kwargs
    )


def create_table_records(
    url: str,
    records: list[dict[str, Any]],
    *,
    protected: frozenset[str] = READ_ONLY_FIELDS,
) -> list[dict[str, Any]]:
    """Create records in any table, ten at a time, and return what Airtable made."""
    created: list[dict[str, Any]] = []
    headers = airtable_headers()

    for batch in chunks(records, 10):
        payload = [
            {
                **record,
                "fields": strip_read_only_fields(
                    record.get("fields", {}), protected
                ),
            }
            for record in batch
        ]

        response = request_with_retry(
            "POST",
            url,
            headers=headers,
            json={"records": payload, "typecast": True},
        )
        created.extend(response.json().get("records", []))
        time.sleep(0.25)

    return created


def update_table_records(
    url: str,
    updates: list[dict[str, Any]],
    *,
    protected: frozenset[str] = READ_ONLY_FIELDS,
) -> None:
    """Patch records in any table, ten at a time."""
    headers = airtable_headers()

    for batch in chunks(updates, 10):
        payload = [
            {
                **update,
                "fields": strip_read_only_fields(
                    update.get("fields", {}), protected
                ),
            }
            for update in batch
        ]

        request_with_retry(
            "PATCH",
            url,
            headers=headers,
            json={"records": payload, "typecast": True},
        )
        time.sleep(0.25)


def fetch_existing_identity_keys() -> set[str]:
    """
    Build the set of dedupe keys already represented in Airtable.

    Uses canonical URL, extracted post ID, and text fingerprint rather than
    the raw URL string, so the same post arriving with tracking parameters or
    from a different Facebook host is recognised as a duplicate.
    """
    records = list_airtable_records(
        fields=[FIELD_URL, FIELD_TEXT, FIELD_USER_NAME]
    )

    keys = build_seen_keys(
        records,
        url_field=FIELD_URL,
        text_field=FIELD_TEXT,
        author_field=FIELD_USER_NAME,
    )

    print(
        f"Loaded {len(records)} Airtable records "
        f"({len(keys)} dedupe keys).",
        flush=True,
    )
    return keys


def select_new_posts(
    posts: list[dict[str, Any]],
    seen_keys: set[str],
) -> list[dict[str, Any]]:
    """Keep only posts that are new by ID, canonical URL, or fingerprint."""
    result = deduplicate_posts(posts, seen_keys)

    print(
        f"Deduplication: {len(result.unique)} new, "
        f"{result.duplicate_count} already present or duplicated.",
        flush=True,
    )
    return result.unique


def map_apify_to_airtable(
    post: dict[str, Any],
    *,
    apify_run_id: str = "",
    scraper_run_record_id: str = "",
) -> dict[str, Any]:
    """
    Map one Apify post onto Airtable fields.

    ``apify_run_id`` and ``scraper_run_record_id`` are what make a post
    traceable: the raw ID for querying, and the linked record for navigating
    from a lead to the scrape that found it (and to its cost).
    """
    user = post.get("user") or {}

    mapped: dict[str, Any] = {
        FIELD_URL: normalize_facebook_url(post.get("url", "")),
        FIELD_FACEBOOK_URL: post.get("facebookUrl", ""),
        FIELD_USER_ID: str(user.get("id", "")),
        FIELD_USER_NAME: user.get("name", ""),
        FIELD_TEXT: post.get("text", ""),
        FIELD_GROUP_TITLE: post.get("groupTitle", ""),
        FIELD_INPUT_URL: post.get("inputUrl", ""),
        FIELD_LIKES: post.get("likesCount", 0) or 0,
        FIELD_COMMENTS: post.get("commentsCount", 0) or 0,
        FIELD_SHARES: post.get("sharesCount", 0) or 0,
    }

    if post.get("time"):
        mapped[FIELD_TIME] = post["time"]

    if apify_run_id:
        mapped[FIELD_APIFY_RUN_ID] = apify_run_id

    if scraper_run_record_id:
        # Linked-record fields take a list of Airtable record IDs.
        mapped[FIELD_SCRAPER_RUN] = [scraper_run_record_id]

    return mapped


def create_new_posts_in_airtable(
    posts: list[dict[str, Any]],
    *,
    apify_run_id: str = "",
    scraper_run_record_id: str = "",
) -> list[str]:
    """
    Create new Airtable records and return their record IDs.

    Every record created here carries the ID of the Apify run that produced
    it and, when the scraper run was logged, a link to that run's record.
    """
    if not posts:
        print("No new Facebook posts to create in Airtable.", flush=True)
        return []

    if DRY_RUN:
        print(
            f"[DRY RUN] Would create {len(posts)} new Airtable records "
            f"attributed to Apify run {apify_run_id or 'unknown'}. "
            f"Nothing written.",
            flush=True,
        )
        for post in posts[:10]:
            print(
                f"[DRY RUN]   {normalize_facebook_url(post.get('url', ''))}",
                flush=True,
            )
        return []

    mapped = [
        {
            "fields": map_apify_to_airtable(
                post,
                apify_run_id=apify_run_id,
                scraper_run_record_id=scraper_run_record_id,
            )
        }
        for post in posts
    ]

    created_records = create_table_records(airtable_url(), mapped)
    created_record_ids = [
        record["id"] for record in created_records if record.get("id")
    ]

    print(
        f"Airtable import complete: {len(created_record_ids)} created, "
        f"attributed to Apify run {apify_run_id or 'unknown'}.",
        flush=True,
    )
    return created_record_ids


# ---------------------------------------------------------------------------
# AI queue selection and prioritisation
# ---------------------------------------------------------------------------

def build_ai_queue_formula() -> str:
    """
    Build the Airtable filter for unprocessed records.

    The Prequalification formula is only required when
    REQUIRE_AIRTABLE_PREQUALIFICATION is enabled; Python prefiltering is
    otherwise authoritative.
    """
    clauses = [
        build_unprocessed_status_clause(),
        f"LEN({{{FIELD_TEXT}}}&'')>0",
        f"LEN({{{FIELD_URL}}}&'')>0",
    ]

    if REQUIRE_AIRTABLE_PREQUALIFICATION:
        clauses.insert(0, f"{{{FIELD_PREQUALIFICATION}}}='Send to AI'")

    return "AND(" + ",".join(clauses) + ")"


def build_unprocessed_status_clause() -> str:
    """
    The AI Status values that mean "this record still needs the model".

    With RETRY_AI_ERRORS enabled, Error records are re-queued too. They are
    not retried forever: process_ai_queue() reads the attempt count recorded
    in AI Output and skips a record that has already used its budget.
    """
    statuses = [
        f"{{{FIELD_AI_STATUS}}}=BLANK()",
        f"{{{FIELD_AI_STATUS}}}='{AI_STATUS_PENDING}'",
    ]

    if RETRY_AI_ERRORS:
        statuses.append(f"{{{FIELD_AI_STATUS}}}='{AI_STATUS_ERROR}'")

    return "OR(" + ",".join(statuses) + ")"


AI_QUEUE_FIELDS = [
    FIELD_URL,
    FIELD_TIME,
    FIELD_USER_NAME,
    FIELD_TEXT,
    FIELD_GROUP_TITLE,
    FIELD_COMMENTS,
    FIELD_PREQUALIFICATION,
    FIELD_AI_STATUS,
    FIELD_AI_OUTPUT,
    # Read so they can be respected. Never written.
    FIELD_HUMAN_DECISION,
    FIELD_OUTREACH_STATUS,
]

# AI Status values. Kept as constants rather than literals so a schema change
# is a one-line edit. These are the values the live base already uses; no new
# single-select option is introduced by this release.
AI_STATUS_PENDING = "Pending"
AI_STATUS_PROCESSED = "Processed"
AI_STATUS_ERROR = "Error"

# Human Decision values.
HUMAN_DECISION_APPROVE = "Approve"
HUMAN_DECISION_REVIEW = "Review"
HUMAN_DECISION_REJECT = "Reject"

#: Outreach Status values that mean the lead is already in the sales funnel.
#: Records in these states are skipped entirely by classification, so a run
#: can never overwrite a recorded outcome.
ACTIVE_OUTREACH_STATUSES = frozenset(
    {
        "contacted",
        "replied",
        "meeting booked",
        "proposal sent",
        "won",
        "lost",
        "no response",
        "do not contact",
    }
)


def human_decision(fields: dict[str, Any]) -> str:
    """Return the human's decision for a record, normalised."""
    return str((fields or {}).get(FIELD_HUMAN_DECISION, "") or "").strip()


def is_human_rejected(fields: dict[str, Any]) -> bool:
    """Did a human explicitly reject this record?"""
    return human_decision(fields).lower() == HUMAN_DECISION_REJECT.lower()


def is_human_approved(fields: dict[str, Any]) -> bool:
    """
    Did a person set Human Decision = "Approve" on this record?

    This is the *only* source of an explicit human override in the pipeline.
    "Review" means someone wants to look at it, not that they have decided, so
    it never bypasses deterministic filtering.
    """
    return human_decision(fields).lower() == HUMAN_DECISION_APPROVE.lower()


def has_active_outreach(fields: dict[str, Any]) -> bool:
    """Is this lead already somewhere in the sales funnel?"""
    status = str(
        (fields or {}).get(FIELD_OUTREACH_STATUS, "") or ""
    ).strip().lower()

    return status in ACTIVE_OUTREACH_STATUSES


#: Where the cross-run AI attempt count is stored.
#:
#: Airtable has no AI Attempts field today, so the count rides in the AI
#: Output JSON blob. Set AIRTABLE_AI_ATTEMPTS_FIELD once a numeric field
#: exists and the store switches to it -- no other code changes, and nothing
#: in the qualification architecture is affected either way.
RETRY_STORE = ai_retry.RetryStore(
    output_field=FIELD_AI_OUTPUT,
    attempts_field=env_str("AIRTABLE_AI_ATTEMPTS_FIELD", ""),
    error_status=AI_STATUS_ERROR,
)


def ai_attempts_so_far(fields: dict[str, Any]) -> int:
    """How many AI attempts this record has already used, across runs."""
    return RETRY_STORE.read_attempts(fields)


def is_retry_exhausted(fields: dict[str, Any]) -> bool:
    """Has this record used up its cross-run AI attempt budget?"""
    return RETRY_STORE.is_exhausted(
        fields,
        max_attempts=AI_ERROR_MAX_ATTEMPTS,
        status_field=FIELD_AI_STATUS,
    )


def is_airtable_prequalified(fields: dict[str, Any]) -> bool:
    """
    Does the Airtable Prequalification *formula* currently say 'Send to AI'?

    This is a machine-generated value computed from the Request, Service, and
    Promotion signal columns. It is not a human decision, and it must never be
    used as one: it cannot bypass the prefilter, lift a hard rejection, or
    approve an expired post. Its only jobs are choosing which records to fetch
    and breaking ties in the queue order.

    See ``is_human_approved`` for the real human override.
    """
    return str(
        (fields or {}).get(FIELD_PREQUALIFICATION, "")
    ).strip().lower() == "send to ai"


def build_prequalified_queue_formula() -> str:
    """Records the Airtable formula currently marks 'Send to AI'."""
    return (
        "AND("
        f"{{{FIELD_PREQUALIFICATION}}}='Send to AI',"
        f"{build_unprocessed_status_clause()},"
        f"LEN({{{FIELD_TEXT}}}&'')>0,"
        f"LEN({{{FIELD_URL}}}&'')>0"
        ")"
    )


def build_record_id_formula(record_ids: Sequence[str]) -> str:
    """Match an explicit set of records by their Airtable record ID."""
    clauses = ",".join(f"RECORD_ID()='{rid}'" for rid in record_ids)
    return (
        "AND("
        f"OR({clauses}),"
        f"{build_unprocessed_status_clause()}"
        ")"
    )


#: Record IDs per RECORD_ID() query. Airtable's formula parameter is a URL
#: query string, so a 200-post scrape has to be asked for in pieces.
RECORD_ID_QUERY_CHUNK = 40


def fetch_records_by_id(record_ids: Sequence[str]) -> list[dict[str, Any]]:
    """
    Fetch specific records by ID, in chunks, preserving nothing about order.

    Used for the current run's own imports so their evaluation cannot depend
    on where they happen to fall in an Airtable page.
    """
    records: list[dict[str, Any]] = []

    for chunk in chunks(list(record_ids), RECORD_ID_QUERY_CHUNK):
        records.extend(
            list_airtable_records(
                formula=build_record_id_formula(chunk),
                fields=AI_QUEUE_FIELDS,
            )
        )

    return records


def build_human_approved_queue_formula() -> str:
    """Records a person set Human Decision = 'Approve' on."""
    return (
        "AND("
        f"{{{FIELD_HUMAN_DECISION}}}='{HUMAN_DECISION_APPROVE}',"
        f"{build_unprocessed_status_clause()},"
        f"LEN({{{FIELD_TEXT}}}&'')>0,"
        f"LEN({{{FIELD_URL}}}&'')>0"
        ")"
    )


def fetch_ai_queue(
    current_run_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """
    Retrieve Airtable records that have never been processed by the AI.

    Everything fetched here gets deterministic evaluation. Fetching is not
    rationing: the OpenAI spend limit is applied later, in process_ai_queue,
    and applies to *calls*, not to records.

    Four phases, in priority order:

    1. Every record imported by this run, fetched by record ID.
    2. Every record a human set Human Decision = 'Approve' on.
    3. Every record the Airtable formula marks 'Send to AI'.
    4. The newest remaining unprocessed backlog, up to PREFILTER_SCAN_LIMIT.

    Phase 1 is the one that matters for a live scrape, and it is unbounded on
    purpose. A run that imports 50 posts evaluates all 50. Before
    2026-08-19 there was no phase 1 at all: the whole queue was capped at
    ``AI_BATCH_LIMIT * 5``, so a run with AI_BATCH_LIMIT=5 evaluated 25 of its
    50 fresh imports and never looked at the other 25 -- one of which was a
    strong lead.

    Fetching this run's imports by ID also removes any dependence on Airtable
    result ordering. Phase 4 is sorted server-side by post time because
    truncating an unsorted query returns records in table order, so a window
    over it is an arbitrary slice.

    Phases 1-3 decide what is *looked at*, never what is sent to the model.
    Every record from every phase goes through the same deterministic
    prefilter, and the non-overridable gate in process_ai_queue applies to all
    of them equally.
    """
    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def take(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add records that are not already selected."""
        fresh = [
            record
            for record in records
            if record.get("id") and record.get("id") not in seen_ids
        ]
        seen_ids.update(record["id"] for record in fresh)
        selected.extend(fresh)
        return fresh

    if current_run_ids:
        imported = take(fetch_records_by_id(current_run_ids))
        print(
            f"Evaluating {len(imported)} of this run's "
            f"{len(current_run_ids)} imported records.",
            flush=True,
        )

    approved = take(
        list_airtable_records(
            formula=build_human_approved_queue_formula(),
            fields=AI_QUEUE_FIELDS,
        )
    )
    if approved:
        print(f"Found {len(approved)} records a human approved.", flush=True)

    prequalified = take(
        list_airtable_records(
            formula=build_prequalified_queue_formula(),
            fields=AI_QUEUE_FIELDS,
        )
    )
    if prequalified:
        print(
            f"Found {len(prequalified)} records the Airtable formula marks "
            f"'Send to AI' (machine signal, not human approval).",
            flush=True,
        )

    remaining = max(PREFILTER_SCAN_LIMIT - len(selected), 0)

    if remaining and not REQUIRE_AIRTABLE_PREQUALIFICATION:
        backlog = take(
            [
                record
                for record in list_airtable_records(
                    formula=build_ai_queue_formula(),
                    fields=AI_QUEUE_FIELDS,
                    max_records=remaining + len(selected),
                    sort_field=FIELD_TIME,
                    sort_direction="desc",
                )
                if record.get("id") not in seen_ids
            ][:remaining]
        )

        print(
            f"Topped up with {len(backlog)} newest unprocessed backlog "
            f"records (scan limit {PREFILTER_SCAN_LIMIT}).",
            flush=True,
        )

    print(
        f"Found {len(selected)} records for deterministic evaluation.",
        flush=True,
    )
    return selected


def prioritize_ai_queue(
    records: list[dict[str, Any]],
    created_record_ids: set[str],
    *,
    now: datetime | None = None,
) -> list[tuple[dict[str, Any], Any]]:
    """
    Order the queue and attach each record's prefilter result.

    Priority order:
      1. Records a human set Human Decision = 'Approve' on
      2. Records imported during this run
      3. Provider requests
      4. Implementation requests
      5. Records the Airtable Prequalification formula marks 'Send to AI'
      6. Strongest Prefilter Score
      7. Newest posts
      8. Least comment competition

    A human's explicit approval outranks everything else: if someone went
    through the table and approved records, those are what the run should
    spend its batch on. The Prequalification formula sits far lower, as the
    machine tiebreaker it is -- ordering only, never eligibility.

    Intent outranks recency on purpose. A three-day-old "looking for someone
    to rebuild our site" is worth more than a fresh vague grumble, and the old
    recency-first ordering let a stale backlog of weak posts eat the batch
    before today's real leads were reached.
    """
    scored: list[tuple[dict[str, Any], PrefilterResult]] = []

    for record in records:
        fields = record.get("fields", {}) or {}
        prefilter = prefilter_post(
            fields.get(FIELD_TEXT),
            post_time=fields.get(FIELD_TIME),
            now=now,
            max_post_age_days=MAX_POST_AGE_DAYS,
            comment_count=fields.get(FIELD_COMMENTS),
            pass_score=PREFILTER_PASS_SCORE,
            allow_tool_research=ALLOW_TOOL_RESEARCH_TO_AI,
        )
        scored.append((record, prefilter))

    def sort_key(
        entry: tuple[dict[str, Any], PrefilterResult],
    ) -> tuple[Any, ...]:
        record, prefilter = entry
        fields = record.get("fields", {}) or {}

        approved = 1 if is_human_approved(fields) else 0
        imported_now = 1 if record.get("id") in created_record_ids else 0
        prequalified = 1 if is_airtable_prequalified(fields) else 0

        provider = 1 if prefilter.intent_type == intent.INTENT_PROVIDER_REQUEST else 0
        implementation = (
            1
            if prefilter.intent_type == intent.INTENT_IMPLEMENTATION_REQUEST
            else 0
        )

        timestamp = parse_post_timestamp(fields.get(FIELD_TIME))
        recency = timestamp.timestamp() if timestamp else 0.0

        try:
            comments = int(fields.get(FIELD_COMMENTS) or 0)
        except (TypeError, ValueError):
            comments = 0

        return (
            -approved,
            -imported_now,
            -provider,
            -implementation,
            -prequalified,
            -prefilter.score,
            -recency,
            comments,
        )

    scored.sort(key=sort_key)
    return scored


# ---------------------------------------------------------------------------
# OpenAI signal extraction
# ---------------------------------------------------------------------------

def extract_signals(fields: dict[str, Any]) -> dict[str, Any]:
    """Ask the model for structured signals about one post."""
    post_text = str(fields.get(FIELD_TEXT, ""))
    if len(post_text) > MAX_POST_CHARS:
        post_text = post_text[:MAX_POST_CHARS]

    post_input = f"""
Extract lead signals from this Facebook post.

POST AUTHOR:
{fields.get(FIELD_USER_NAME, "")}

POST DATE:
{fields.get(FIELD_TIME, "")}

FACEBOOK GROUP:
{fields.get(FIELD_GROUP_TITLE, "")}

POST TEXT:
{post_text}

POST URL:
{fields.get(FIELD_URL, "")}
""".strip()

    last_error: Exception | None = None
    client = get_openai_client()

    for attempt in range(1, OPENAI_MAX_ATTEMPTS + 1):
        try:
            token_limit = OPENAI_MAX_OUTPUT_TOKENS + ((attempt - 1) * 1000)

            response = client.responses.create(
                model=OPENAI_MODEL,
                instructions=SYSTEM_INSTRUCTIONS,
                input=post_input,
                reasoning={"effort": "low"},
                text={
                    "verbosity": "low",
                    "format": {
                        "type": "json_schema",
                        "name": "brucetech_lead_signals",
                        "schema": LEAD_SCHEMA,
                        "strict": True,
                    },
                },
                max_output_tokens=token_limit,
            )

            status = getattr(response, "status", None)
            if status == "failed":
                raise RuntimeError(
                    f"OpenAI response failed: {getattr(response, 'error', None)}"
                )

            if status == "incomplete":
                raise RuntimeError(
                    "OpenAI response incomplete: "
                    f"{getattr(response, 'incomplete_details', None)}"
                )

            output_text = response.output_text
            if not output_text:
                raise RuntimeError("OpenAI returned an empty response.")

            return json.loads(output_text)

        except Exception as exc:
            last_error = exc

            if attempt == OPENAI_MAX_ATTEMPTS:
                break

            wait_seconds = OPENAI_RETRY_DELAY_SECONDS * attempt
            print(
                f"OpenAI attempt {attempt} failed: {exc}. "
                f"Retrying in {wait_seconds}s...",
                flush=True,
            )
            time.sleep(wait_seconds)

    raise RuntimeError(
        f"OpenAI signal extraction failed after "
        f"{OPENAI_MAX_ATTEMPTS} attempts: {last_error}"
    )


def collect_policy_disqualifiers(fields: dict[str, Any]) -> list[str]:
    """
    Rejections that come from pipeline policy rather than the post.

    A suppressed author, an excluded group, or a human's explicit Reject all
    veto a lead no matter how well it scores.
    """
    codes: list[str] = []

    author = str(fields.get(FIELD_USER_NAME, "") or "").strip().lower()
    if author and author in SUPPRESSED_AUTHORS:
        codes.append(REJECT_SUPPRESSED_AUTHOR)

    group = str(fields.get(FIELD_GROUP_TITLE, "") or "").strip().lower()
    if group and group in DISALLOWED_GROUPS:
        codes.append(REJECT_DISALLOWED_GROUP)

    if is_human_rejected(fields):
        codes.append(REJECT_HUMAN_REJECTED)

    return codes


def qualify_post(
    fields: dict[str, Any],
    *,
    prefilter: PrefilterResult | None = None,
    now: datetime | None = None,
    human_override: bool = False,
) -> tuple[LeadDecision, dict[str, Any]]:
    """
    Extract signals with the AI, then decide deterministically in Python.

    The deterministic intent classification is handed to ``evaluate_lead``
    so it can veto general-advice posts and gate outreach. The model's
    signals only ever feed the score.

    Returns the decision plus the raw signals for logging.
    """
    signals = extract_signals(fields)

    decision = evaluate_lead(
        signals,
        suggested_dm=signals.get("suggested_dm", ""),
        recommended_channel=signals.get(
            "recommended_channel", "do_not_contact"
        ),
        post_time=fields.get(FIELD_TIME),
        now=now,
        threshold=QUALIFICATION_THRESHOLD,
        manual_review_threshold=MANUAL_REVIEW_THRESHOLD,
        hot_threshold=HOT_LEAD_THRESHOLD,
        max_post_age_days=MAX_POST_AGE_DAYS,
        intent_type=prefilter.intent_type if prefilter else None,
        post_text=fields.get(FIELD_TEXT),
        min_outreach_confidence=MIN_OUTREACH_CONFIDENCE,
        business_pain_min_score=BUSINESS_PAIN_OUTREACH_MIN_SCORE,
        extra_disqualifiers=collect_policy_disqualifiers(fields),
        human_override=human_override,
    )

    return decision, signals


# ---------------------------------------------------------------------------
# Airtable writes
# ---------------------------------------------------------------------------

def build_ai_output(
    signals: dict[str, Any],
    decision: LeadDecision,
    prefilter: PrefilterResult | None,
) -> str:
    """
    Build the AI Output audit blob.

    Holds the model's raw signals plus the Python-side assessment. The
    deterministic intent lives here because the Airtable ``Intent Type``
    column is a formula and cannot be written to.
    """
    payload: dict[str, Any] = {
        "qualification_version": QUALIFICATION_VERSION,
        "model": OPENAI_MODEL,
        "lead_score": decision.lead_score,
        "tier": decision.tier,
        "score_breakdown": decision.score_breakdown,
        "signals": signals,
    }

    if prefilter is not None:
        payload["intent_type"] = prefilter.intent_type
        payload["intent_label"] = prefilter.intent_label
        payload["prefilter_score"] = prefilter.score
        payload["prefilter_breakdown"] = prefilter.breakdown
        payload["service_categories"] = prefilter.service_categories
        # How the service match was established. "adjacent" means Python
        # inferred fit and the model's answer is what settles it.
        payload["match_basis"] = prefilter.match_basis

    try:
        return json.dumps(payload, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return json.dumps({"qualification_version": QUALIFICATION_VERSION})


def map_decision_to_airtable(
    decision: LeadDecision,
    signals: dict[str, Any],
    *,
    prefilter_score: int | None = None,
    prefilter: PrefilterResult | None = None,
) -> dict[str, Any]:
    """Build the Airtable field payload for a processed record."""
    summary = str(signals.get("lead_summary", "") or "")

    if decision.tier == TIER_MANUAL_REVIEW:
        # Manual-review records are never outreach-ready and must be obvious
        # to a human scanning the table.
        summary = f"[MANUAL REVIEW] {summary}".strip()

    payload: dict[str, Any] = {
        FIELD_AI_STATUS: "Processed",
        FIELD_QUALIFIED: bool(decision.qualified),
        FIELD_LEAD_SCORE: int(decision.lead_score),
        FIELD_SERVICE_MATCH: signals.get("service_match") or "None",
        FIELD_LEAD_SUMMARY: summary,
        FIELD_REJECTION_REASON: decision.rejection_reason or "None",
        FIELD_SUGGESTED_DM: decision.suggested_dm,
        FIELD_RECOMMENDED_CHANNEL: decision.recommended_channel,
        FIELD_EVIDENCE: str(signals.get("evidence", "") or ""),
        FIELD_LEAD_TIER: decision.tier,
        FIELD_MANUAL_REVIEW: bool(decision.manual_review),
        FIELD_OUTREACH_READY: bool(decision.outreach_ready),
        FIELD_DISQUALIFIERS: ", ".join(decision.hard_rejection_codes) or "",
        FIELD_QUALIFICATION_VERSION: QUALIFICATION_VERSION,
        FIELD_AI_OUTPUT: build_ai_output(signals, decision, prefilter),
    }

    if prefilter_score is not None:
        payload[FIELD_PREFILTER_SCORE] = int(prefilter_score)

    return payload


def _strip_extended_fields(
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stripped: list[dict[str, Any]] = []

    for update in updates:
        fields = {
            key: value
            for key, value in (update.get("fields") or {}).items()
            if key not in EXTENDED_FIELDS
        }
        stripped.append({**update, "fields": fields})

    return stripped


_EXTENDED_FIELDS_AVAILABLE = True


def update_airtable_records(updates: list[dict[str, Any]]) -> None:
    """
    Patch Airtable records.

    If the Airtable base does not yet have the fields added by this release,
    the write is retried without them and a warning is printed once.
    """
    global _EXTENDED_FIELDS_AVAILABLE

    if not updates:
        return

    if DRY_RUN:
        for update in updates:
            fields = update.get("fields", {}) or {}
            print(
                f"[DRY RUN] Would update {update.get('id')}: "
                f"tier={fields.get(FIELD_LEAD_TIER)} "
                f"score={fields.get(FIELD_LEAD_SCORE)} "
                f"qualified={fields.get(FIELD_QUALIFIED)} "
                f"outreach={fields.get(FIELD_OUTREACH_READY)} "
                f"dm={'yes' if fields.get(FIELD_SUGGESTED_DM) else 'no'}",
                flush=True,
            )
        return

    url = airtable_url()
    headers = airtable_headers()

    safe_updates = [
        {**update, "fields": strip_read_only_fields(update.get("fields", {}))}
        for update in updates
    ]

    for batch in chunks(safe_updates, 10):
        payload = batch if _EXTENDED_FIELDS_AVAILABLE else (
            _strip_extended_fields(batch)
        )

        try:
            request_with_retry(
                "PATCH",
                url,
                headers=headers,
                json={"records": payload, "typecast": True},
            )
        except RuntimeError as exc:
            if (
                _EXTENDED_FIELDS_AVAILABLE
                and "UNKNOWN_FIELD_NAME" in str(exc)
            ):
                print(
                    "Airtable is missing the new fields "
                    f"({', '.join(EXTENDED_FIELDS)}). Continuing without "
                    "them. Create them in Airtable to enable tiering and "
                    "manual-review views. See README.md.",
                    file=sys.stderr,
                    flush=True,
                )
                _EXTENDED_FIELDS_AVAILABLE = False

                request_with_retry(
                    "PATCH",
                    url,
                    headers=headers,
                    json={
                        "records": _strip_extended_fields(batch),
                        "typecast": True,
                    },
                )
            else:
                raise

        time.sleep(0.25)


#: Re-exported so callers and tests have one import site for retry policy.
TRANSIENT_ERROR_MARKERS = ai_retry.TRANSIENT_ERROR_MARKERS
is_transient_error = ai_retry.is_transient_error


def build_error_payload(
    error_message: str,
    *,
    attempts: int,
    transient: bool,
) -> dict[str, Any]:
    """Build the Airtable payload for a failed record."""
    return RETRY_STORE.build_failure_fields(
        error_message,
        attempts=attempts,
        transient=transient,
        status_field=FIELD_AI_STATUS,
        version=QUALIFICATION_VERSION,
    )


def mark_ai_error(
    record_id: str,
    error_message: str,
    *,
    attempts: int = 1,
    transient: bool = True,
) -> None:
    """Record an AI failure without destroying the record's lead fields."""
    if DRY_RUN:
        print(
            f"[DRY RUN] Would mark {record_id} as Error "
            f"(attempt {attempts}, transient={transient}): "
            f"{error_message[:120]}",
            flush=True,
        )
        return

    update_table_records(
        airtable_url(),
        [
            {
                "id": record_id,
                "fields": build_error_payload(
                    error_message, attempts=attempts, transient=transient
                ),
            }
        ],
    )


def build_prefilter_rejection_update(
    record_id: str,
    prefilter: PrefilterResult,
    *,
    extra_codes: Sequence[str] = (),
) -> dict[str, Any]:
    """
    Build the Airtable payload for a post rejected before the AI call.

    These records cost nothing. The intent classification and the machine
    -readable rejection codes are recorded so a human can see *why* the post
    never reached the model without having to re-read it -- a stale record
    now says STALE_POST in Disqualifiers, exactly as a post-AI rejection
    would.
    """
    codes = list(dict.fromkeys([*prefilter.rejection_codes, *extra_codes]))

    reason = " ".join(prefilter.reasons) or "Rejected by deterministic prefilter."
    if REJECT_HUMAN_REJECTED in extra_codes:
        reason = HARD_REJECTION_REASONS[REJECT_HUMAN_REJECTED]

    diagnostics = {
        "qualification_version": QUALIFICATION_VERSION,
        "prefiltered": True,
        "intent_type": prefilter.intent_type,
        "intent_label": prefilter.intent_label,
        "service_matched": prefilter.service_matched,
        "service_categories": prefilter.service_categories,
        "prefilter_score": prefilter.score,
        "prefilter_breakdown": prefilter.breakdown,
        "reasons": prefilter.reasons,
        "rejection_codes": codes,
        "match_basis": prefilter.match_basis,
        "ai_called": False,
    }

    return {
        "id": record_id,
        "fields": {
            FIELD_AI_STATUS: AI_STATUS_PROCESSED,
            FIELD_QUALIFIED: False,
            FIELD_LEAD_SCORE: 0,
            FIELD_SERVICE_MATCH: "None",
            FIELD_REJECTION_REASON: reason[:500],
            FIELD_SUGGESTED_DM: "",
            FIELD_RECOMMENDED_CHANNEL: "do_not_contact",
            FIELD_LEAD_TIER: TIER_REJECTED,
            FIELD_MANUAL_REVIEW: False,
            FIELD_OUTREACH_READY: False,
            FIELD_DISQUALIFIERS: ", ".join(codes),
            FIELD_PREFILTER_SCORE: int(prefilter.score),
            FIELD_QUALIFICATION_VERSION: QUALIFICATION_VERSION,
            FIELD_AI_OUTPUT: json.dumps(
                diagnostics, indent=2, sort_keys=True, default=str
            ),
        },
    }


# ---------------------------------------------------------------------------
# AI queue processing
# ---------------------------------------------------------------------------

@dataclass
class RunSummary:
    """Counters for one pipeline execution, printed at the end."""

    apify_run_id: str = ""
    dataset_id: str = ""
    dataset_items: int = 0
    new_leads: int = 0
    duplicates_skipped: int = 0
    queue_size: int = 0
    skipped_in_funnel: int = 0
    skipped_retry_exhausted: int = 0
    prefilter_accepted: int = 0
    prefilter_rejected: int = 0
    #: Rejected before the AI call because the post was past MAX_POST_AGE_DAYS.
    #: Counted inside prefilter_rejected; broken out so a run that spends
    #: nothing on expired leads says so on its own line.
    stale_skipped: int = 0
    human_rejected: int = 0
    provider_requests: int = 0
    implementation_requests: int = 0
    business_pain: int = 0
    tool_research: int = 0
    ai_processed: int = 0
    ai_errors: int = 0
    hot: int = 0
    qualified: int = 0
    manual_review: int = 0
    rejected: int = 0
    outreach_ready: int = 0

    def render(self) -> str:
        return "\n".join(
            [
                "-" * 62,
                "RUN SUMMARY",
                "-" * 62,
                f"  Apify run ID          {self.apify_run_id or 'n/a'}",
                f"  Dataset ID            {self.dataset_id or 'n/a'}",
                f"  Dataset items         {self.dataset_items}",
                f"  New unique leads      {self.new_leads}",
                f"  Duplicates skipped    {self.duplicates_skipped}",
                "",
                f"  AI queue size         {self.queue_size}",
                f"  Skipped (in funnel)   {self.skipped_in_funnel}",
                f"  Skipped (retries up)  {self.skipped_retry_exhausted}",
                f"  Prefilter accepted    {self.prefilter_accepted}",
                f"  Prefilter rejected    {self.prefilter_rejected}",
                f"  Stale (no AI call)    {self.stale_skipped}",
                f"  Human rejected        {self.human_rejected}",
                "",
                f"  Provider requests     {self.provider_requests}",
                f"  Implementation reqs   {self.implementation_requests}",
                f"  Business pain         {self.business_pain}",
                f"  Tool research         {self.tool_research}",
                "",
                f"  AI processed          {self.ai_processed}",
                f"  AI errors             {self.ai_errors}",
                f"  Hot                   {self.hot}",
                f"  Qualified             {self.qualified}",
                f"  Manual review         {self.manual_review}",
                f"  Rejected              {self.rejected}",
                f"  Outreach ready        {self.outreach_ready}",
                "-" * 62,
            ]
        )


def process_ai_queue(
    prioritized: list[tuple[dict[str, Any], PrefilterResult]],
    *,
    summary: RunSummary | None = None,
    now: datetime | None = None,
) -> RunSummary:
    """
    Work the prioritised queue: prefilter, then AI, then deterministic scoring.

    Records already in the sales funnel are skipped outright -- reclassifying
    a lead someone has already contacted would overwrite the sales record for
    no benefit.
    """
    summary = summary or RunSummary()
    summary.queue_size = len(prioritized)

    if not prioritized:
        print("No records require AI qualification.", flush=True)
        return summary

    pending_updates: list[dict[str, Any]] = []

    def flush(force: bool = False) -> None:
        nonlocal pending_updates
        if pending_updates and (force or len(pending_updates) >= 10):
            update_airtable_records(pending_updates)
            pending_updates = []

    for index, (record, prefilter) in enumerate(prioritized, start=1):
        record_id = record["id"]
        fields = record.get("fields", {}) or {}
        author = fields.get(FIELD_USER_NAME, "")
        post_url = fields.get(FIELD_URL, "")

        # Count what the deterministic classifier saw, whatever happens next.
        if prefilter.intent_type == intent.INTENT_PROVIDER_REQUEST:
            summary.provider_requests += 1
        elif prefilter.intent_type == intent.INTENT_IMPLEMENTATION_REQUEST:
            summary.implementation_requests += 1
        elif prefilter.intent_type == intent.INTENT_BUSINESS_PAIN:
            summary.business_pain += 1
        elif prefilter.intent_type == intent.INTENT_TOOL_RESEARCH:
            summary.tool_research += 1

        # Never touch a lead that is already in the sales funnel.
        if has_active_outreach(fields):
            summary.skipped_in_funnel += 1
            print(
                f"[{index}/{len(prioritized)}] Skipped: already "
                f"{fields.get(FIELD_OUTREACH_STATUS)!r} in the funnel.",
                flush=True,
            )
            continue

        # A record that has burned its cross-run retry budget is left alone.
        if is_retry_exhausted(fields):
            summary.skipped_retry_exhausted += 1
            print(
                f"[{index}/{len(prioritized)}] Skipped: "
                f"{AI_ERROR_MAX_ATTEMPTS} failed AI attempts already.",
                flush=True,
            )
            continue

        # A human's explicit Approve overrides the intent heuristics only.
        # It is read from Human Decision, never from the machine-generated
        # Prequalification formula.
        human_approved = is_human_approved(fields)

        # A human's explicit Reject ends it here. Asking the model about a
        # record someone has already turned down cannot change the outcome,
        # so it must not cost a call.
        if is_human_rejected(fields):
            summary.human_rejected += 1
            summary.rejected += 1
            pending_updates.append(
                build_prefilter_rejection_update(
                    record_id,
                    prefilter,
                    extra_codes=[REJECT_HUMAN_REJECTED],
                )
            )
            print(
                f"[{index}/{len(prioritized)}] Skipped: Human Decision is "
                f"{HUMAN_DECISION_REJECT!r}. URL={post_url}",
                flush=True,
            )
            flush()
            continue

        # Non-overridable pre-AI gate.
        #
        # STALE_POST is the reason this exists. An expired post is rejected
        # by evaluate_lead() anyway, so sending it to the model buys nothing
        # and costs a call -- which is exactly what happened in production on
        # 2026-08-18, when six formula-prequalified records skipped the
        # prefilter and five posts between 17 and 25 days old were paid for
        # before being rejected as stale.
        #
        # This runs before ENFORCE_PYTHON_PREFILTER, before the human
        # override, and before get_openai_client(). Nothing lifts it.
        blocking_codes = prefilter.non_overridable_codes
        if blocking_codes:
            if prefilter.is_stale:
                summary.stale_skipped += 1
            summary.prefilter_rejected += 1
            summary.rejected += 1
            pending_updates.append(
                build_prefilter_rejection_update(record_id, prefilter)
            )
            print(
                f"[{index}/{len(prioritized)}] Blocked before AI "
                f"({', '.join(blocking_codes)}) "
                f"intent={prefilter.intent_type} "
                f"score={prefilter.score} URL={post_url}",
                flush=True,
            )
            flush()
            continue

        # Deterministic prefilter: never spend a token on an obvious reject.
        # Past this point a human Approve may let a record through, because
        # everything still standing is a heuristic judgement -- intent, or a
        # Prefilter Score below the minimum.
        if (
            ENFORCE_PYTHON_PREFILTER
            and not prefilter.passed
            and not human_approved
        ):
            summary.prefilter_rejected += 1
            summary.rejected += 1
            pending_updates.append(
                build_prefilter_rejection_update(record_id, prefilter)
            )
            print(
                f"[{index}/{len(prioritized)}] Prefiltered out "
                f"(intent={prefilter.intent_type} "
                f"service={prefilter.service_matched} "
                f"score={prefilter.score}) URL={post_url}",
                flush=True,
            )
            flush()
            continue

        summary.prefilter_accepted += 1

        if summary.ai_processed >= AI_BATCH_LIMIT:
            continue

        try:
            summary.ai_processed += 1
            decision, signals = qualify_post(
                fields,
                prefilter=prefilter,
                now=now,
                human_override=human_approved,
            )

            if decision.tier == "Hot":
                summary.hot += 1
            elif decision.tier == "Qualified":
                summary.qualified += 1
            elif decision.tier == TIER_MANUAL_REVIEW:
                summary.manual_review += 1
            else:
                summary.rejected += 1

            if decision.outreach_ready:
                summary.outreach_ready += 1

            pending_updates.append(
                {
                    "id": record_id,
                    "fields": map_decision_to_airtable(
                        decision,
                        signals,
                        prefilter_score=prefilter.score,
                        prefilter=prefilter,
                    ),
                }
            )

            print(
                f"[{index}/{len(prioritized)}] "
                f"Score={decision.lead_score} "
                f"Tier={decision.tier} "
                f"Intent={prefilter.intent_type} "
                f"Qualified={decision.qualified} "
                f"Outreach={decision.outreach_ready} "
                f"Author={author!r} URL={post_url}",
                flush=True,
            )

            if decision.hard_rejection_codes:
                print(
                    f"    Hard rejections: "
                    f"{', '.join(decision.hard_rejection_codes)}",
                    flush=True,
                )

            flush()

        except Exception as exc:  # noqa: BLE001 - one bad record must not
            # abort the batch; the failure is recorded and bounded.
            summary.ai_errors += 1
            transient = is_transient_error(exc)

            # A permanent failure consumes the whole budget immediately, so
            # it is never retried. A transient one gets another go next run.
            attempts = RETRY_STORE.next_attempt_count(
                fields,
                transient=transient,
                max_attempts=AI_ERROR_MAX_ATTEMPTS,
            )

            print(
                f"[{index}/{len(prioritized)}] AI processing failed for "
                f"{record_id} (attempt {attempts}/{AI_ERROR_MAX_ATTEMPTS}, "
                f"transient={transient}): {exc}",
                file=sys.stderr,
                flush=True,
            )

            try:
                mark_ai_error(
                    record_id,
                    str(exc),
                    attempts=attempts,
                    transient=transient,
                )
            except Exception as mark_exc:  # noqa: BLE001 - best effort
                print(
                    f"Could not mark {record_id} as Error: {mark_exc}",
                    file=sys.stderr,
                    flush=True,
                )

    flush(force=True)
    return summary


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def validate_airtable_schema() -> None:
    """
    Best-effort preflight check that the Raw Signals fields exist.

    Uses the Airtable metadata API. Tokens without the ``schema.bases:read``
    scope cannot read it, in which case the check is skipped with a note
    rather than blocking the run -- the write path still degrades gracefully.

    This never creates, renames, or deletes anything. It only reads.
    """
    base_id = require_env("AIRTABLE_BASE_ID")
    table_name = require_env("AIRTABLE_TABLE_NAME")

    try:
        response = request_with_retry(
            "GET",
            f"https://api.airtable.com/v0/meta/bases/{base_id}/tables",
            headers=airtable_headers(),
            max_attempts=2,
        )
        tables = (response.json() or {}).get("tables", [])
    except Exception as exc:  # noqa: BLE001 - preflight must never block
        print(
            f"Skipping Airtable schema validation ({exc}). The token most "
            f"likely lacks the schema.bases:read scope.",
            flush=True,
        )
        return

    table = next(
        (
            item
            for item in tables
            if str(item.get("name", "")).strip() == table_name.strip()
        ),
        None,
    )

    if table is None:
        raise RuntimeError(
            f"Airtable table {table_name!r} was not found in base {base_id}. "
            f"Check AIRTABLE_TABLE_NAME."
        )

    present = {str(field.get("name", "")).strip() for field in table.get("fields", [])}
    missing = [name for name in REQUIRED_RAW_SIGNAL_FIELDS if name not in present]

    if missing:
        raise RuntimeError(
            f"Airtable table {table_name!r} is missing required fields: "
            f"{', '.join(missing)}. Create them in Airtable (see README.md) "
            f"and re-run. No records were modified."
        )

    print(
        f"Airtable schema validated: all {len(REQUIRED_RAW_SIGNAL_FIELDS)} "
        f"required fields present in {table_name!r}.",
        flush=True,
    )


def log_scraper_run(
    run: apify_runs.ApifyRun,
) -> scraper_runs.ScraperRunRecord | None:
    """Upsert the Facebook Post Scraper Runs record for this Apify run."""
    if not LOG_SCRAPER_RUNS:
        print("Scraper run logging is disabled.", flush=True)
        return None

    table_url = airtable_table_url(SCRAPER_RUNS_TABLE)

    try:
        return scraper_runs.upsert_scraper_run(
            run,
            lister=list_scraper_run_records,
            # Scraper Runs shares no column names with Raw Signals.
            creator=lambda records: create_table_records(
                table_url, records, protected=SCRAPER_RUNS_PROTECTED_FIELDS
            ),
            updater=lambda updates: update_table_records(
                table_url, updates, protected=SCRAPER_RUNS_PROTECTED_FIELDS
            ),
            run_input=fetch_apify_run_input(run),
            dry_run=DRY_RUN,
        )
    except Exception as exc:  # noqa: BLE001 - logging must not lose the import
        print(
            f"Could not log Apify run {run.id} to {SCRAPER_RUNS_TABLE}: "
            f"{exc}. Imported posts will still carry the run ID.",
            file=sys.stderr,
            flush=True,
        )
        return None


def scraper_run_dates() -> dict[str, str]:
    """Map each scraper-run record ID to its Run Date, for Last Run."""
    try:
        records = list_scraper_run_records(
            fields=[scraper_runs.FIELD_RUN_DATE]
        )
    except Exception as exc:  # noqa: BLE001 - Last Run is optional
        print(f"Could not read scraper run dates: {exc}", flush=True)
        return {}

    dates: dict[str, str] = {}

    for record in records:
        record_id = record.get("id")
        run_date = (record.get("fields", {}) or {}).get(
            scraper_runs.FIELD_RUN_DATE
        )

        if record_id and run_date:
            dates[str(record_id)] = str(run_date)

    return dates


RAW_SIGNAL_FIELD_NAMES = group_performance.RawSignalFieldNames(
    group_title=FIELD_GROUP_TITLE,
    text=FIELD_TEXT,
    qualified=FIELD_QUALIFIED,
    outreach_ready=FIELD_OUTREACH_READY,
    outreach_status=FIELD_OUTREACH_STATUS,
    legacy_contacted=FIELD_CONTACTED_LEGACY,
    evidence=FIELD_EVIDENCE,
    intent_type=FIELD_INTENT_TYPE,
    scraper_run=FIELD_SCRAPER_RUN,
)


def refresh_group_performance() -> None:
    """Recompute the Facebook Group Performance operational counts."""
    if not UPDATE_GROUP_PERFORMANCE:
        print("Group performance refresh is disabled.", flush=True)
        return

    performance_url = airtable_table_url(GROUP_PERFORMANCE_TABLE)

    try:
        group_performance.refresh_group_performance(
            raw_signal_lister=list_airtable_records,
            performance_lister=list_group_performance_records,
            performance_updater=lambda updates: update_table_records(
                performance_url,
                updates,
                protected=group_performance.NEVER_WRITE_FIELDS,
            ),
            fields=RAW_SIGNAL_FIELD_NAMES,
            run_dates=scraper_run_dates(),
            dry_run=DRY_RUN,
        )
    except Exception as exc:  # noqa: BLE001 - reporting must not fail the run
        print(
            f"Could not refresh {GROUP_PERFORMANCE_TABLE}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def main() -> None:
    started_at = datetime.now(timezone.utc)

    print(
        f"Starting BruceTech lead pipeline at {started_at.isoformat()}",
        flush=True,
    )
    print(
        f"Qualification {QUALIFICATION_VERSION}: "
        f"hot>={HOT_LEAD_THRESHOLD}, "
        f"qualified>={QUALIFICATION_THRESHOLD}, "
        f"manual review>={MANUAL_REVIEW_THRESHOLD}, "
        f"max post age={MAX_POST_AGE_DAYS}d, "
        f"prefilter pass>={PREFILTER_PASS_SCORE}, "
        f"min outreach confidence={MIN_OUTREACH_CONFIDENCE}.",
        flush=True,
    )

    if DRY_RUN:
        print(
            "DRY RUN: Airtable will not be modified and no Apify task will "
            "be started. Records will be read and scored, and the intended "
            "writes printed.",
            flush=True,
        )

    summary = RunSummary()

    if not DRY_RUN:
        validate_airtable_schema()

    # 1. Resolve exactly one Apify run and read that run's own dataset.
    #    In dry-run mode a paid task run is never started; the last
    #    successful run is read instead.
    run = resolve_apify_run(
        start_new_run=APIFY_START_NEW_RUN and not DRY_RUN
    )

    if DRY_RUN and APIFY_START_NEW_RUN:
        print(
            "DRY RUN: skipped starting a new (paid) Apify task run; read the "
            f"last successful run {run.id} instead.",
            flush=True,
        )

    apify_posts = collect_apify_posts(run)

    summary.apify_run_id = run.id
    summary.dataset_id = run.dataset_id
    summary.dataset_items = run.item_count or 0

    # 2. Log the run, then import only posts that are new by ID, canonical
    #    URL, or fingerprint -- each linked to the run that produced it.
    scraper_run = log_scraper_run(run)

    seen_keys = fetch_existing_identity_keys()
    new_posts = select_new_posts(apify_posts, seen_keys)

    summary.new_leads = len(new_posts)
    summary.duplicates_skipped = len(apify_posts) - len(new_posts)

    created_record_ids = create_new_posts_in_airtable(
        new_posts,
        apify_run_id=run.id,
        scraper_run_record_id=scraper_run.record_id if scraper_run else "",
    )

    if created_record_ids:
        print("Waiting for Airtable formulas to calculate...", flush=True)
        time.sleep(AIRTABLE_FORMULA_WAIT_SECONDS)
    else:
        print("No new Facebook posts were imported.", flush=True)

    # 3. Prioritise the backlog, then qualify.
    ai_records = fetch_ai_queue(created_record_ids)
    prioritized = prioritize_ai_queue(ai_records, set(created_record_ids))
    process_ai_queue(prioritized, summary=summary)

    # 4. Refresh the measurement loop.
    refresh_group_performance()

    completed_at = datetime.now(timezone.utc)
    duration = (completed_at - started_at).total_seconds()

    print(summary.render(), flush=True)
    print(
        f"BruceTech pipeline completed at {completed_at.isoformat()} "
        f"in {duration:.1f}s.",
        flush=True,
    )


if __name__ == "__main__":
    main()
