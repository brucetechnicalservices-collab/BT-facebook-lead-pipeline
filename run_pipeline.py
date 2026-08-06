"""
BruceTech Facebook lead pipeline.

Flow:

1.  Pull posts from Apify (either the last successful run, or a fresh task
    run started and awaited by this script).
2.  Deduplicate against Airtable using post ID, canonical URL, and text
    fingerprint, then import only genuinely new posts.
3.  Screen the backlog with a deterministic Python prefilter.
4.  Ask the AI to extract *structured signals only*.
5.  Score, tier, and qualify in Python. The AI never decides qualification.

Environment variables are read lazily so this module can be imported by the
test suite without any credentials present.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import quote

import requests

from normalization import (
    build_seen_keys,
    deduplicate_posts,
    normalize_facebook_url,
)
from qualification import (
    DEFAULT_HOT_THRESHOLD,
    DEFAULT_MANUAL_REVIEW_THRESHOLD,
    DEFAULT_MAX_POST_AGE_DAYS,
    DEFAULT_QUALIFICATION_THRESHOLD,
    KNOWN_DISQUALIFIER_CODES,
    SERVICE_CATEGORIES,
    TIER_MANUAL_REVIEW,
    WEBSITE_OPPORTUNITY_TYPES,
    WEBSITE_PLATFORMS,
    LeadDecision,
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


def require_env(name: str) -> str:
    """Return a required environment variable or fail with a clear message."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable {name} is not set."
        )
    return value


OPENAI_MODEL = env_str("OPENAI_MODEL", "gpt-5-mini")

AI_BATCH_LIMIT = env_int("AI_BATCH_LIMIT", 20)

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
APIFY_START_NEW_RUN = env_bool("APIFY_START_NEW_RUN", False)
APIFY_RUN_TIMEOUT_SECONDS = env_int("APIFY_RUN_TIMEOUT_SECONDS", 900)
APIFY_RUN_POLL_SECONDS = env_int("APIFY_RUN_POLL_SECONDS", 15)

#: The Airtable Prequalification formula is undocumented and easy to break.
#: Python prefiltering is now authoritative; set this to true to additionally
#: require the Airtable formula to say "Send to AI".
REQUIRE_AIRTABLE_PREQUALIFICATION = env_bool(
    "REQUIRE_AIRTABLE_PREQUALIFICATION", False
)

#: Skip the AI call entirely for posts the deterministic prefilter rejects.
ENFORCE_PYTHON_PREFILTER = env_bool("ENFORCE_PYTHON_PREFILTER", True)

#: Website analysis focus. Narrows the entire run to website work for
#: business owners and business pages: expensive Shopify/Wix plans that would
#: be cheaper on WordPress, dated sites needing a redesign, broken sites, and
#: businesses with customers but no website. Everything else is rejected.
WEBSITE_FOCUS_MODE = env_bool("WEBSITE_FOCUS_MODE", False)

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

# Fields added by this release. Create these in Airtable before deploying;
# see README.md. If they are missing, writes fall back to the core fields.
FIELD_LEAD_TIER = "Lead Tier"
FIELD_MANUAL_REVIEW = "Manual Review"
FIELD_OUTREACH_READY = "Outreach Ready"
FIELD_DISQUALIFIERS = "Disqualifiers"
FIELD_PREFILTER_SCORE = "Prefilter Score"

# Website analysis fields. Written on every run so the data is there when you
# switch focus mode on.
FIELD_WEBSITE_OPPORTUNITY = "Website Opportunity"
FIELD_WEBSITE_PLATFORM = "Website Platform"

EXTENDED_FIELDS = (
    FIELD_LEAD_TIER,
    FIELD_MANUAL_REVIEW,
    FIELD_OUTREACH_READY,
    FIELD_DISQUALIFIERS,
    FIELD_PREFILTER_SCORE,
    FIELD_WEBSITE_OPPORTUNITY,
    FIELD_WEBSITE_PLATFORM,
)


def airtable_url() -> str:
    base_id = require_env("AIRTABLE_BASE_ID")
    table = require_env("AIRTABLE_TABLE_NAME")
    return (
        "https://api.airtable.com/v0/"
        f"{base_id}/{quote(table, safe='')}"
    )


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

Analyze only the supplied public Facebook post and metadata. Never invent
business names, locations, urgency, budgets, website problems, or technical
details that are not present in the post.

If the post does not support a signal, report the lowest or "unknown" value.
Do not guess upward. Under-reporting is safer than over-reporting.

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

website_opportunity - the kind of website work the post points to, or "none"
  when the post is not about a website at all:
  no_website                = a business with customers but no website, often
                              running on a Facebook page alone
  expensive_platform        = paying a monthly plan (Shopify, Wix, Squarespace)
                              they consider too costly
  platform_migration        = wants to move off their current platform
  outdated_website          = the site is old, dated, or unmaintained
  broken_or_failing_website = down, slow, erroring, or not mobile friendly
  redesign_or_refresh       = wants the existing site redesigned
  new_website_build         = wants a site built
  ecommerce_store           = wants to sell online
  seo_or_visibility         = cannot be found, ranking or traffic problems
  maintenance_or_updates    = ongoing edits, updates, or support
  other_website_need        = a website need none of the above describes

website_platform - the platform the business is on today, only when the post
  actually says so. Use "none" when they state they have no website and
  "unknown" when it is not mentioned. Never infer a platform from the type of
  business.

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


# Appended to the instructions only when the run is in website analysis focus
# mode. It changes what the model pays attention to; it still does not decide
# anything. Python applies the focus-mode rules.
WEBSITE_FOCUS_INSTRUCTIONS = """

WEBSITE ANALYSIS FOCUS

This run is scoped to website work only. The reader is a business owner or
the person running a business page.

BruceTech's website offers, in the order they matter for this run:

1. Replacing an expensive hosted plan. Businesses on Shopify, Wix, or
   Squarespace pay a monthly fee forever. BruceTech rebuilds the same store
   or site on WordPress or WooCommerce so the recurring cost drops sharply.
   Any mention of platform fees, plan costs, or transaction fees matters.
2. Redesigning old websites. Sites that look dated, were built years ago, or
   were abandoned by whoever made them.
3. Fixing existing websites. Down, slow, erroring, insecure, unable to be
   edited, not mobile friendly, broken forms, checkout, or booking.
4. Building a first website for a business that already has customers.
   Businesses trading through a Facebook page alone, by phone, or by DM are
   exactly this case.
5. Any other website need: e-commerce, SEO and visibility, hosting, domains,
   maintenance, speed, or ongoing edits.

Set website_opportunity and website_platform carefully -- in this run they
drive the outcome. Report "none" when the post is not about a website; that
is a correct answer and the post will be rejected without penalty to you.

Report only what the post says. Do not assume a business has a bad website,
an expensive plan, or no website because the post does not mention one, and
never claim to have looked at their site.
""".rstrip()


def build_system_instructions(*, website_focus: bool = False) -> str:
    """Return the model instructions for this run."""
    if website_focus:
        return SYSTEM_INSTRUCTIONS + WEBSITE_FOCUS_INSTRUCTIONS

    return SYSTEM_INSTRUCTIONS


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
        "website_opportunity": _enum(WEBSITE_OPPORTUNITY_TYPES),
        "website_platform": _enum(WEBSITE_PLATFORMS),
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
        "website_opportunity",
        "website_platform",
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


APIFY_DATASET_FIELDS = (
    "legacyId,url,time,text,user,groupTitle,"
    "facebookUrl,inputUrl,likesCount,"
    "commentsCount,sharesCount,error,errorDescription"
)


def _valid_apify_posts(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise RuntimeError("Apify returned an unexpected response.")

    posts = [
        item
        for item in data
        if isinstance(item, dict)
        and item.get("url")
        and not item.get("error")
    ]

    print(
        f"Fetched {len(data)} Apify items; {len(posts)} valid posts.",
        flush=True,
    )
    return posts


# ---------------------------------------------------------------------------
# Apify
# ---------------------------------------------------------------------------

def fetch_latest_apify_posts() -> list[dict[str, Any]]:
    """Read the dataset from the task's last successful run."""
    task_id = require_env("APIFY_TASK_ID")

    url = (
        "https://api.apify.com/v2/actor-tasks/"
        f"{task_id}/runs/last/dataset/items"
    )

    params = {
        "token": require_env("APIFY_TOKEN"),
        "status": "SUCCEEDED",
        "format": "json",
        "clean": "true",
        "fields": APIFY_DATASET_FIELDS,
    }

    response = request_with_retry("GET", url, params=params)
    return _valid_apify_posts(response.json())


def start_apify_task_run() -> dict[str, Any]:
    """Start a fresh Apify task run and return the run object."""
    task_id = require_env("APIFY_TASK_ID")
    token = require_env("APIFY_TOKEN")

    url = f"https://api.apify.com/v2/actor-tasks/{task_id}/runs"

    response = request_with_retry("POST", url, params={"token": token})
    run = (response.json() or {}).get("data") or {}

    run_id = run.get("id")
    if not run_id:
        raise RuntimeError("Apify did not return a run ID.")

    print(f"Started Apify run {run_id}.", flush=True)
    return run


def wait_for_apify_run(
    run_id: str,
    *,
    timeout_seconds: int | None = None,
    poll_seconds: int | None = None,
) -> dict[str, Any]:
    """Poll an Apify run until it reaches a terminal state."""
    token = require_env("APIFY_TOKEN")
    timeout = timeout_seconds or APIFY_RUN_TIMEOUT_SECONDS
    interval = max(poll_seconds or APIFY_RUN_POLL_SECONDS, 1)

    url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    deadline = time.monotonic() + timeout

    while True:
        response = request_with_retry("GET", url, params={"token": token})
        run = (response.json() or {}).get("data") or {}
        status = run.get("status")

        if status == "SUCCEEDED":
            print(f"Apify run {run_id} succeeded.", flush=True)
            return run

        if status in {"FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"}:
            raise RuntimeError(
                f"Apify run {run_id} finished with status {status}."
            )

        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Apify run {run_id} did not finish within {timeout}s "
                f"(last status: {status})."
            )

        print(
            f"Apify run {run_id} status={status}; "
            f"checking again in {interval}s...",
            flush=True,
        )
        time.sleep(interval)


def fetch_apify_dataset_items(dataset_id: str) -> list[dict[str, Any]]:
    """Read items from one specific Apify dataset."""
    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"

    params = {
        "token": require_env("APIFY_TOKEN"),
        "format": "json",
        "clean": "true",
        "fields": APIFY_DATASET_FIELDS,
    }

    response = request_with_retry("GET", url, params=params)
    return _valid_apify_posts(response.json())


def collect_apify_posts() -> list[dict[str, Any]]:
    """
    Return Apify posts using the configured strategy.

    With APIFY_START_NEW_RUN enabled, a fresh run is started, awaited, and
    that exact run's dataset is read -- avoiding the race where
    ``runs/last`` returns a previous run's data.
    """
    if not APIFY_START_NEW_RUN:
        return fetch_latest_apify_posts()

    run = start_apify_task_run()
    finished = wait_for_apify_run(str(run.get("id")))

    dataset_id = finished.get("defaultDatasetId") or run.get(
        "defaultDatasetId"
    )
    if not dataset_id:
        raise RuntimeError("Apify run did not expose a default dataset ID.")

    return fetch_apify_dataset_items(str(dataset_id))


# ---------------------------------------------------------------------------
# Airtable reads and deduplication
# ---------------------------------------------------------------------------

def list_airtable_records(
    *,
    formula: str | None = None,
    fields: list[str] | None = None,
    max_records: int | None = None,
    sort_field: str | None = None,
    sort_direction: str = "desc",
) -> list[dict[str, Any]]:
    """
    Page through Airtable records.

    ``sort_field`` matters whenever ``max_records`` is set: without a sort,
    Airtable returns records in the table's own order, so truncating gives an
    arbitrary slice rather than the most relevant one.
    """
    records: list[dict[str, Any]] = []
    offset: str | None = None
    url = airtable_url()
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


def map_apify_to_airtable(post: dict[str, Any]) -> dict[str, Any]:
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

    return mapped


def create_new_posts_in_airtable(posts: list[dict[str, Any]]) -> list[str]:
    """Create new Airtable records and return their record IDs."""
    if not posts:
        print("No new Facebook posts to create in Airtable.", flush=True)
        return []

    if DRY_RUN:
        print(
            f"[DRY RUN] Would create {len(posts)} new Airtable records. "
            f"Nothing written.",
            flush=True,
        )
        for post in posts[:10]:
            print(
                f"[DRY RUN]   {normalize_facebook_url(post.get('url', ''))}",
                flush=True,
            )
        return []

    created_record_ids: list[str] = []
    mapped = [{"fields": map_apify_to_airtable(post)} for post in posts]
    url = airtable_url()
    headers = airtable_headers()

    for batch_number, batch in enumerate(chunks(mapped, 10), start=1):
        response = request_with_retry(
            "POST",
            url,
            headers=headers,
            json={"records": batch, "typecast": True},
        )

        created_records = response.json().get("records", [])
        created_record_ids.extend(
            record["id"] for record in created_records if record.get("id")
        )

        print(
            f"Airtable create batch {batch_number}: "
            f"{len(created_records)} new records.",
            flush=True,
        )
        time.sleep(0.25)

    print(
        f"Airtable import complete: {len(created_record_ids)} created.",
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
        f"OR({{{FIELD_AI_STATUS}}}=BLANK(),{{{FIELD_AI_STATUS}}}='Pending')",
        f"LEN({{{FIELD_TEXT}}}&'')>0",
        f"LEN({{{FIELD_URL}}}&'')>0",
    ]

    if REQUIRE_AIRTABLE_PREQUALIFICATION:
        clauses.insert(0, f"{{{FIELD_PREQUALIFICATION}}}='Send to AI'")

    return "AND(" + ",".join(clauses) + ")"


AI_QUEUE_FIELDS = [
    FIELD_URL,
    FIELD_TIME,
    FIELD_USER_NAME,
    FIELD_TEXT,
    FIELD_GROUP_TITLE,
    FIELD_PREQUALIFICATION,
    FIELD_AI_STATUS,
]


def is_human_flagged(fields: dict[str, Any]) -> bool:
    """Did a human mark this record 'Send to AI' in Airtable?"""
    return str(
        (fields or {}).get(FIELD_PREQUALIFICATION, "")
    ).strip().lower() == "send to ai"


def build_prequalified_queue_formula() -> str:
    """Records a human explicitly flagged as 'Send to AI'."""
    return (
        "AND("
        f"{{{FIELD_PREQUALIFICATION}}}='Send to AI',"
        f"OR({{{FIELD_AI_STATUS}}}=BLANK(),{{{FIELD_AI_STATUS}}}='Pending'),"
        f"LEN({{{FIELD_TEXT}}}&'')>0,"
        f"LEN({{{FIELD_URL}}}&'')>0"
        ")"
    )


def fetch_ai_queue() -> list[dict[str, Any]]:
    """
    Retrieve Airtable records that have never been processed by the AI.

    Fetched in two phases so a curated Prequalification selection is never
    lost behind an arbitrary page of the backlog:

    1. Every record flagged 'Send to AI' by a human.
    2. The newest remaining unprocessed records, to top up the window.

    Phase 2 is sorted server-side by post time. Without that sort, Airtable
    returns records in table order, so truncating to a window produced an
    arbitrary slice and the prioritisation below could only reorder that
    slice.
    """
    prequalified = list_airtable_records(
        formula=build_prequalified_queue_formula(),
        fields=AI_QUEUE_FIELDS,
    )

    if prequalified:
        print(
            f"Found {len(prequalified)} records flagged 'Send to AI'.",
            flush=True,
        )

    seen_ids = {record.get("id") for record in prequalified}
    window = max(AI_BATCH_LIMIT * 5, AI_BATCH_LIMIT)
    remaining = max(window - len(prequalified), 0)

    backlog: list[dict[str, Any]] = []
    if remaining and not REQUIRE_AIRTABLE_PREQUALIFICATION:
        backlog = [
            record
            for record in list_airtable_records(
                formula=build_ai_queue_formula(),
                fields=AI_QUEUE_FIELDS,
                max_records=remaining + len(prequalified),
                sort_field=FIELD_TIME,
                sort_direction="desc",
            )
            if record.get("id") not in seen_ids
        ][:remaining]

        print(
            f"Topped up with {len(backlog)} newest unprocessed records.",
            flush=True,
        )

    records = prequalified + backlog

    print(f"Found {len(records)} unprocessed Airtable records.", flush=True)
    return records


def prioritize_ai_queue(
    records: list[dict[str, Any]],
    created_record_ids: set[str],
    *,
    now: datetime | None = None,
) -> list[tuple[dict[str, Any], Any]]:
    """
    Order the queue and attach each record's prefilter result.

    Priority order:
      1. Records a human flagged 'Send to AI' in Airtable
      2. Records imported during this run
      3. Newest posts
      4. Strongest website-opportunity signal (focus mode only)
      5. Strongest deterministic prefilter score
      6. Highest buying-intent signal

    A human's explicit selection outranks everything else: if someone went
    through the table and marked records, those are what the run should
    spend its batch on.
    """
    scored: list[tuple[dict[str, Any], Any]] = []

    for record in records:
        fields = record.get("fields", {}) or {}
        prefilter = prefilter_post(
            fields.get(FIELD_TEXT),
            post_time=fields.get(FIELD_TIME),
            now=now,
            max_post_age_days=MAX_POST_AGE_DAYS,
            website_focus=WEBSITE_FOCUS_MODE,
        )
        scored.append((record, prefilter))

    def sort_key(entry: tuple[dict[str, Any], Any]) -> tuple[Any, ...]:
        record, prefilter = entry
        fields = record.get("fields", {}) or {}

        prequalified = 1 if is_human_flagged(fields) else 0

        imported_now = 1 if record.get("id") in created_record_ids else 0

        timestamp = parse_post_timestamp(fields.get(FIELD_TIME))
        recency = timestamp.timestamp() if timestamp else 0.0

        return (
            -prequalified,
            -imported_now,
            -recency,
            # Always 0 outside website focus mode, so ordering is unchanged.
            -prefilter.website_score,
            -prefilter.score,
            -prefilter.intent_score,
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
                instructions=build_system_instructions(
                    website_focus=WEBSITE_FOCUS_MODE
                ),
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


def qualify_post(
    fields: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[LeadDecision, dict[str, Any]]:
    """
    Extract signals with the AI, then decide deterministically in Python.

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
        website_focus=WEBSITE_FOCUS_MODE,
    )

    return decision, signals


# ---------------------------------------------------------------------------
# Airtable writes
# ---------------------------------------------------------------------------

def map_decision_to_airtable(
    decision: LeadDecision,
    signals: dict[str, Any],
    *,
    prefilter_score: int | None = None,
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
        FIELD_WEBSITE_OPPORTUNITY: decision.website_opportunity,
        FIELD_WEBSITE_PLATFORM: decision.website_platform,
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

    for batch in chunks(updates, 10):
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


def mark_ai_error(record_id: str, error_message: str) -> None:
    if DRY_RUN:
        print(
            f"[DRY RUN] Would mark {record_id} as Error: "
            f"{error_message[:120]}",
            flush=True,
        )
        return

    request_with_retry(
        "PATCH",
        airtable_url(),
        headers=airtable_headers(),
        json={
            "records": [
                {
                    "id": record_id,
                    "fields": {
                        FIELD_AI_STATUS: "Error",
                        FIELD_REJECTION_REASON: error_message[:500],
                    },
                }
            ],
            "typecast": True,
        },
    )


def build_prefilter_rejection_update(
    record_id: str,
    prefilter: Any,
) -> dict[str, Any]:
    """Build the Airtable payload for a post rejected before the AI call."""
    reason = " ".join(prefilter.reasons) or "Rejected by deterministic prefilter."

    return {
        "id": record_id,
        "fields": {
            FIELD_AI_STATUS: "Processed",
            FIELD_QUALIFIED: False,
            FIELD_LEAD_SCORE: 0,
            FIELD_SERVICE_MATCH: "None",
            FIELD_REJECTION_REASON: reason[:500],
            FIELD_SUGGESTED_DM: "",
            FIELD_RECOMMENDED_CHANNEL: "do_not_contact",
            FIELD_LEAD_TIER: "Rejected",
            FIELD_MANUAL_REVIEW: False,
            FIELD_OUTREACH_READY: False,
            FIELD_PREFILTER_SCORE: int(prefilter.score),
        },
    }


# ---------------------------------------------------------------------------
# AI queue processing
# ---------------------------------------------------------------------------

def process_ai_queue(
    prioritized: list[tuple[dict[str, Any], Any]],
    *,
    now: datetime | None = None,
) -> None:
    if not prioritized:
        print("No records require AI qualification.", flush=True)
        return

    pending_updates: list[dict[str, Any]] = []
    counts = {
        "hot": 0,
        "qualified": 0,
        "manual_review": 0,
        "rejected": 0,
        "prefiltered": 0,
        "errors": 0,
    }
    processed_by_ai = 0

    for index, (record, prefilter) in enumerate(prioritized, start=1):
        record_id = record["id"]
        fields = record.get("fields", {}) or {}
        author = fields.get(FIELD_USER_NAME, "")
        post_url = fields.get(FIELD_URL, "")

        # A human's explicit 'Send to AI' overrides the keyword prefilter.
        # Someone reviewed this record; do not silently reject it on a
        # keyword miss.
        human_flagged = is_human_flagged(fields)

        # Deterministic prefilter: never spend a token on an obvious reject.
        if (
            ENFORCE_PYTHON_PREFILTER
            and not prefilter.passed
            and not human_flagged
        ):
            counts["prefiltered"] += 1
            counts["rejected"] += 1
            pending_updates.append(
                build_prefilter_rejection_update(record_id, prefilter)
            )
            print(
                f"[{index}/{len(prioritized)}] Prefiltered out "
                f"(score={prefilter.score}) URL={post_url}",
                flush=True,
            )

            if len(pending_updates) == 10:
                update_airtable_records(pending_updates)
                pending_updates = []
            continue

        if processed_by_ai >= AI_BATCH_LIMIT:
            continue

        try:
            processed_by_ai += 1
            decision, signals = qualify_post(fields, now=now)

            if decision.tier == "Hot":
                counts["hot"] += 1
            elif decision.tier == "Qualified":
                counts["qualified"] += 1
            elif decision.tier == TIER_MANUAL_REVIEW:
                counts["manual_review"] += 1
            else:
                counts["rejected"] += 1

            pending_updates.append(
                {
                    "id": record_id,
                    "fields": map_decision_to_airtable(
                        decision,
                        signals,
                        prefilter_score=prefilter.score,
                    ),
                }
            )

            website_note = (
                f"Website={decision.website_opportunity}"
                f"/{decision.website_platform} "
                if WEBSITE_FOCUS_MODE
                else ""
            )

            print(
                f"[{index}/{len(prioritized)}] "
                f"Score={decision.lead_score} "
                f"Tier={decision.tier} "
                f"Qualified={decision.qualified} "
                f"Outreach={decision.outreach_ready} "
                f"{website_note}"
                f"Author={author!r} URL={post_url}",
                flush=True,
            )

            if decision.hard_rejection_codes:
                print(
                    f"    Hard rejections: "
                    f"{', '.join(decision.hard_rejection_codes)}",
                    flush=True,
                )

            if len(pending_updates) == 10:
                update_airtable_records(pending_updates)
                pending_updates = []

        except Exception as exc:
            counts["errors"] += 1
            print(
                f"[{index}/{len(prioritized)}] AI processing failed for "
                f"{record_id}: {exc}",
                file=sys.stderr,
                flush=True,
            )

            try:
                mark_ai_error(record_id, str(exc))
            except Exception as mark_exc:
                print(
                    f"Could not mark {record_id} as Error: {mark_exc}",
                    file=sys.stderr,
                    flush=True,
                )

    update_airtable_records(pending_updates)

    print(
        "AI processing complete: "
        f"{counts['hot']} hot, "
        f"{counts['qualified']} qualified, "
        f"{counts['manual_review']} manual review, "
        f"{counts['rejected']} rejected "
        f"({counts['prefiltered']} prefiltered), "
        f"{counts['errors']} errors.",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    started_at = datetime.now(timezone.utc)

    print(
        f"Starting BruceTech lead pipeline at {started_at.isoformat()}",
        flush=True,
    )
    print(
        f"Thresholds: hot>={HOT_LEAD_THRESHOLD}, "
        f"qualified>={QUALIFICATION_THRESHOLD}, "
        f"manual review>={MANUAL_REVIEW_THRESHOLD}, "
        f"max post age={MAX_POST_AGE_DAYS}d.",
        flush=True,
    )

    if WEBSITE_FOCUS_MODE:
        print(
            "WEBSITE ANALYSIS FOCUS: this run scores website work only "
            "(platform cost conversions, redesigns, fixes, and businesses "
            "with no website). Posts with no website need are rejected as "
            "NO_WEBSITE_OPPORTUNITY.",
            flush=True,
        )

    if DRY_RUN:
        print(
            "DRY RUN: Airtable will not be modified. Records will be read "
            "and scored, and the intended writes printed.",
            flush=True,
        )

    # 1. Collect posts from Apify.
    apify_posts = collect_apify_posts()

    # 2. Import only posts that are new by ID, canonical URL, or fingerprint.
    seen_keys = fetch_existing_identity_keys()
    new_posts = select_new_posts(apify_posts, seen_keys)
    created_record_ids = create_new_posts_in_airtable(new_posts)

    if created_record_ids:
        print("Waiting for Airtable formulas to calculate...", flush=True)
        time.sleep(AIRTABLE_FORMULA_WAIT_SECONDS)
    else:
        print("No new Facebook posts were imported.", flush=True)

    # 3. Prioritise the backlog, then qualify.
    ai_records = fetch_ai_queue()
    prioritized = prioritize_ai_queue(ai_records, set(created_record_ids))
    process_ai_queue(prioritized)

    completed_at = datetime.now(timezone.utc)
    duration = (completed_at - started_at).total_seconds()

    print(
        f"BruceTech pipeline completed at {completed_at.isoformat()} "
        f"in {duration:.1f}s.",
        flush=True,
    )


if __name__ == "__main__":
    main()
