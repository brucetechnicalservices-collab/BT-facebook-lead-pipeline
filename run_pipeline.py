from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import quote

import requests
from openai import OpenAI


# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------

APIFY_TOKEN = os.environ["APIFY_TOKEN"]
APIFY_TASK_ID = os.environ["APIFY_TASK_ID"]

AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_TABLE_NAME = os.environ["AIRTABLE_TABLE_NAME"]

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

AI_BATCH_LIMIT = int(os.getenv("AI_BATCH_LIMIT", "20"))
QUALIFICATION_THRESHOLD = int(
    os.getenv("QUALIFICATION_THRESHOLD", "55")
)
AIRTABLE_FORMULA_WAIT_SECONDS = int(
    os.getenv("AIRTABLE_FORMULA_WAIT_SECONDS", "15")
)
OPENAI_MAX_OUTPUT_TOKENS = int(
    os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "2500")
)
OPENAI_MAX_ATTEMPTS = int(
    os.getenv("OPENAI_MAX_ATTEMPTS", "3")
)
OPENAI_RETRY_DELAY_SECONDS = int(
    os.getenv("OPENAI_RETRY_DELAY_SECONDS", "5")
)
MAX_POST_CHARS = int(os.getenv("MAX_POST_CHARS", "8000"))


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


AIRTABLE_URL = (
    "https://api.airtable.com/v0/"
    f"{AIRTABLE_BASE_ID}/{quote(AIRTABLE_TABLE_NAME, safe='')}"
)

AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json",
}

OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY)


# ---------------------------------------------------------------------------
# OpenAI qualification instructions
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTIONS = """
You are BruceTech's Facebook lead qualification analyst.

BruceTech is a Toronto-based web development, managed IT,
Microsoft 365, cybersecurity, AI consulting, and business
automation company.

BruceTech services include:

Website and e-commerce:
- Website design and development
- Website redesign
- WordPress, Shopify, and WooCommerce support
- Landing pages
- Booking systems
- Payment and Stripe integrations
- E-commerce
- SEO
- Website maintenance
- Business email and domain setup

Managed IT:
- Managed IT support
- Microsoft 365
- Outlook and business email
- Email migrations
- SharePoint, OneDrive, and Teams
- Device support
- Network and Wi-Fi
- VPN
- Backups
- Cybersecurity
- User onboarding and permissions

AI, workflow, and consulting:
- AI automation consultation
- AI readiness assessment
- Workflow automation
- Business-process consultation and process mapping
- CRM implementation and automation
- Airtable, Make, and Zapier workflows
- API and system integrations
- Lead qualification and customer follow-up automation
- Reporting and data-entry automation
- Document and order-processing automation
- Appointment, intake, and onboarding automation
- AI chatbots, receptionists, and internal assistants
- Knowledge-base assistants
- Business-process optimization
- Custom AI workflow planning and implementation
- Ongoing AI and automation support

Analyze only the supplied public Facebook post and metadata.
Do not invent facts, business names, locations, urgency, budgets,
website problems, or technical details.

A qualified lead must:
- Explicitly or strongly imply that help is needed
- Have credible business or organizational context
- Match at least one BruceTech service
- Appear unresolved
- Be appropriate for respectful outreach

Posts can show buying intent indirectly. Examples include:
- Asking what software, CRM, POS, booking, payment, or website tool to use
- Asking for recommendations
- Describing a repetitive manual process
- Discussing a switch from an existing system
- Asking how to connect, integrate, or automate systems
- Complaining about missed calls, follow-ups, booking, payments, or IT issues

Reject:
- Businesses advertising their own products or services
- Freelancers, agencies, or consultants promoting themselves
- Job seekers or people offering labour
- Students and educational questions
- Personal consumer requests
- Spam or affiliate promotions
- General discussions without meaningful buying intent
- Requests already resolved or where a provider was selected
- Posts unrelated to BruceTech services
- Posts asking only for free work

Scoring:
- Explicit request for help, recommendation, or provider: +30
- Clear BruceTech service match: +25
- Recent and likely actionable: +15
- Identifiable business or organization: +10
- Toronto, GTA, Ontario, or Canadian relevance: +10
- Urgency or meaningful business impact: +10
- DIY or educational discussion: -25
- Competitor, agency, job seeker, or promotional post: -40
- Resolved request: -40
- No credible business context: -25
- Unrelated request: -40

A score of 55 or higher may be qualified.
Do not force a post to reach the threshold.

For qualified leads:
- Write a natural Facebook DM under 80 words
- Mention only details supported by the post
- Include brucetech.ca naturally
- End with one simple question
- Do not mention scraping, monitoring, tracking, or AI analysis
- Do not claim BruceTech reviewed the person's website or systems

For rejected posts:
- suggested_dm must be an empty string
- recommended_channel must be do_not_contact

Always return every structured-output field.
Use "None" when service_match or rejection_reason does not apply.
""".strip()


LEAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "qualified": {"type": "boolean"},
        "lead_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
        "service_match": {"type": "string"},
        "lead_summary": {"type": "string"},
        "rejection_reason": {"type": "string"},
        "suggested_dm": {"type": "string"},
        "recommended_channel": {
            "type": "string",
            "enum": [
                "direct_message",
                "public_reply_then_dm",
                "do_not_contact",
            ],
        },
        "evidence": {"type": "string"},
    },
    "required": [
        "qualified",
        "lead_score",
        "service_match",
        "lead_summary",
        "rejection_reason",
        "suggested_dm",
        "recommended_channel",
        "evidence",
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
            response = requests.request(
                method,
                url,
                timeout=120,
                **kwargs,
            )
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
# ---------------------------------------------------------------------------

def fetch_latest_apify_posts() -> list[dict[str, Any]]:
    url = (
        "https://api.apify.com/v2/actor-tasks/"
        f"{APIFY_TASK_ID}/runs/last/dataset/items"
    )

    params = {
        "token": APIFY_TOKEN,
        "status": "SUCCEEDED",
        "format": "json",
        "clean": "true",
        "fields": (
            "legacyId,url,time,text,user,groupTitle,"
            "facebookUrl,inputUrl,likesCount,"
            "commentsCount,sharesCount,error,errorDescription"
        ),
    }

    response = request_with_retry("GET", url, params=params)
    data = response.json()

    if not isinstance(data, list):
        raise RuntimeError("Apify returned an unexpected response.")

    posts = [
        item
        for item in data
        if item.get("url")
        and item.get("legacyId")
        and not item.get("error")
    ]

    print(
        f"Fetched {len(data)} Apify items; "
        f"{len(posts)} valid Facebook posts.",
        flush=True,
    )
    return posts


# ---------------------------------------------------------------------------
# Airtable reads and new-record filtering
# ---------------------------------------------------------------------------

def list_airtable_records(
    *,
    formula: str | None = None,
    fields: list[str] | None = None,
    max_records: int | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset: str | None = None

    while True:
        params: list[tuple[str, str | int]] = [("pageSize", 100)]

        if formula:
            params.append(("filterByFormula", formula))

        if fields:
            for field in fields:
                params.append(("fields[]", field))

        if offset:
            params.append(("offset", offset))

        response = request_with_retry(
            "GET",
            AIRTABLE_URL,
            headers=AIRTABLE_HEADERS,
            params=params,
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


def fetch_existing_facebook_urls() -> set[str]:
    """
    Read only Airtable's Url field and return all existing post URLs.

    This allows the pipeline to create only unseen posts instead of upserting
    every item from the latest Apify dataset.
    """
    records = list_airtable_records(fields=[FIELD_URL])

    existing_urls = {
        str(record.get("fields", {}).get(FIELD_URL, "")).strip()
        for record in records
        if record.get("fields", {}).get(FIELD_URL)
    }

    print(
        f"Loaded {len(existing_urls)} existing Facebook URLs "
        f"from Airtable.",
        flush=True,
    )
    return existing_urls


def select_new_posts(
    posts: list[dict[str, Any]],
    existing_urls: set[str],
) -> list[dict[str, Any]]:
    """
    Keep only posts whose URL is not already in Airtable.

    Also removes duplicate URLs inside the current Apify dataset.
    """
    new_posts: list[dict[str, Any]] = []
    seen_in_run: set[str] = set()

    for post in posts:
        url = str(post.get("url", "")).strip()

        if not url:
            continue

        if url in existing_urls or url in seen_in_run:
            continue

        seen_in_run.add(url)
        new_posts.append(post)

    print(
        f"New-post check: {len(new_posts)} new, "
        f"{len(posts) - len(new_posts)} already present or duplicated.",
        flush=True,
    )
    return new_posts


def map_apify_to_airtable(
    post: dict[str, Any],
) -> dict[str, Any]:
    user = post.get("user") or {}

    mapped: dict[str, Any] = {
        FIELD_URL: post.get("url", ""),
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


def create_new_posts_in_airtable(
    posts: list[dict[str, Any]],
) -> list[str]:
    """
    Create only new Airtable records and return their Airtable record IDs.

    Those IDs are later used to ensure only records created in this workflow
    run can be sent to OpenAI.
    """
    if not posts:
        print("No new Facebook posts to create in Airtable.", flush=True)
        return []

    created_record_ids: list[str] = []
    mapped = [
        {"fields": map_apify_to_airtable(post)}
        for post in posts
    ]

    for batch_number, batch in enumerate(chunks(mapped, 10), start=1):
        response = request_with_retry(
            "POST",
            AIRTABLE_URL,
            headers=AIRTABLE_HEADERS,
            json={
                "records": batch,
                "typecast": True,
            },
        )

        result = response.json()
        created_records = result.get("records", [])
        created_record_ids.extend(
            record["id"]
            for record in created_records
            if record.get("id")
        )

        print(
            f"Airtable create batch {batch_number}: "
            f"{len(created_records)} new records.",
            flush=True,
        )
        time.sleep(0.25)

    print(
        f"Airtable new-record import complete: "
        f"{len(created_record_ids)} created.",
        flush=True,
    )
    return created_record_ids


# ---------------------------------------------------------------------------
# AI queue: process all unprocessed Airtable backlog records
# ---------------------------------------------------------------------------

def fetch_ai_queue() -> list[dict[str, Any]]:
    """
    Retrieve Airtable records that passed formula prequalification and have
    never been processed by OpenAI.

    This includes:
    - Newly imported records from the current workflow run
    - Older backlog records whose AI Status is blank or Pending

    Records with AI Status = Processed or Error are not reprocessed.
    The number processed per workflow run is capped by AI_BATCH_LIMIT.
    """
    formula = (
        "AND("
        f"{{{FIELD_PREQUALIFICATION}}}='Send to AI',"
        "OR("
        f"{{{FIELD_AI_STATUS}}}=BLANK(),"
        f"{{{FIELD_AI_STATUS}}}='Pending'"
        "),"
        f"LEN({{{FIELD_TEXT}}}&'')>0,"
        f"LEN({{{FIELD_URL}}}&'')>0"
        ")"
    )

    fields = [
        FIELD_URL,
        FIELD_TIME,
        FIELD_USER_NAME,
        FIELD_TEXT,
        FIELD_GROUP_TITLE,
        FIELD_PREQUALIFICATION,
        FIELD_AI_STATUS,
    ]

    records = list_airtable_records(
        formula=formula,
        fields=fields,
        max_records=AI_BATCH_LIMIT,
    )

    print(
        f"Found {len(records)} unprocessed Airtable records "
        f"eligible for AI (limit: {AI_BATCH_LIMIT}).",
        flush=True,
    )
    return records


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

def qualify_post(
    fields: dict[str, Any],
) -> dict[str, Any]:
    post_text = str(fields.get(FIELD_TEXT, ""))
    if len(post_text) > MAX_POST_CHARS:
        post_text = post_text[:MAX_POST_CHARS]

    post_input = f"""
Evaluate this Facebook post as a potential BruceTech business lead.

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

    for attempt in range(1, OPENAI_MAX_ATTEMPTS + 1):
        try:
            token_limit = OPENAI_MAX_OUTPUT_TOKENS + ((attempt - 1) * 1000)

            response = OPENAI_CLIENT.responses.create(
                model=OPENAI_MODEL,
                instructions=SYSTEM_INSTRUCTIONS,
                input=post_input,
                reasoning={"effort": "low"},
                text={
                    "verbosity": "low",
                    "format": {
                        "type": "json_schema",
                        "name": "brucetech_lead_result",
                        "schema": LEAD_SCHEMA,
                        "strict": True,
                    },
                },
                max_output_tokens=token_limit,
            )

            status = getattr(response, "status", None)
            if status == "failed":
                error = getattr(response, "error", None)
                raise RuntimeError(
                    f"OpenAI response failed: {error}"
                )

            if status == "incomplete":
                details = getattr(
                    response,
                    "incomplete_details",
                    None,
                )
                raise RuntimeError(
                    f"OpenAI response incomplete: {details}"
                )

            output_text = response.output_text
            if not output_text:
                raise RuntimeError("OpenAI returned an empty response.")

            result = json.loads(output_text)

            score = int(result.get("lead_score", 0))
            result["lead_score"] = max(0, min(score, 100))

            if result["lead_score"] < QUALIFICATION_THRESHOLD:
                result["qualified"] = False

            if not result.get("qualified", False):
                result["qualified"] = False
                result["suggested_dm"] = ""
                result["recommended_channel"] = "do_not_contact"

                if not result.get("service_match"):
                    result["service_match"] = "None"

                if not result.get("rejection_reason"):
                    result["rejection_reason"] = (
                        "The post did not meet the qualification threshold."
                    )
            elif not result.get("rejection_reason"):
                result["rejection_reason"] = "None"

            return result

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
        f"OpenAI qualification failed after "
        f"{OPENAI_MAX_ATTEMPTS} attempts: {last_error}"
    )


def map_ai_result_to_airtable(
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        FIELD_AI_STATUS: "Processed",
        FIELD_QUALIFIED: bool(result["qualified"]),
        FIELD_LEAD_SCORE: int(result["lead_score"]),
        FIELD_SERVICE_MATCH: (
            result.get("service_match") or "None"
        ),
        FIELD_LEAD_SUMMARY: result.get("lead_summary", ""),
        FIELD_REJECTION_REASON: (
            result.get("rejection_reason") or "None"
        ),
        FIELD_SUGGESTED_DM: result.get("suggested_dm", ""),
        FIELD_RECOMMENDED_CHANNEL: result.get(
            "recommended_channel",
            "do_not_contact",
        ),
        FIELD_EVIDENCE: result.get("evidence", ""),
    }


def update_airtable_records(
    updates: list[dict[str, Any]],
) -> None:
    if not updates:
        return

    for batch in chunks(updates, 10):
        request_with_retry(
            "PATCH",
            AIRTABLE_URL,
            headers=AIRTABLE_HEADERS,
            json={
                "records": batch,
                "typecast": True,
            },
        )
        time.sleep(0.25)


def mark_ai_error(
    record_id: str,
    error_message: str,
) -> None:
    request_with_retry(
        "PATCH",
        AIRTABLE_URL,
        headers=AIRTABLE_HEADERS,
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


def process_ai_queue(
    records: list[dict[str, Any]],
) -> None:
    if not records:
        print("No new records require OpenAI qualification.", flush=True)
        return

    pending_updates: list[dict[str, Any]] = []
    qualified_count = 0
    rejected_count = 0
    error_count = 0

    for index, record in enumerate(records, start=1):
        record_id = record["id"]
        fields = record.get("fields", {})
        author = fields.get(FIELD_USER_NAME, "")
        post_url = fields.get(FIELD_URL, "")

        try:
            result = qualify_post(fields)

            if result["qualified"]:
                qualified_count += 1
            else:
                rejected_count += 1

            pending_updates.append(
                {
                    "id": record_id,
                    "fields": map_ai_result_to_airtable(result),
                }
            )

            print(
                f"[{index}/{len(records)}] "
                f"Score={result['lead_score']} "
                f"Qualified={result['qualified']} "
                f"Author={author!r} URL={post_url}",
                flush=True,
            )

            if len(pending_updates) == 10:
                update_airtable_records(pending_updates)
                pending_updates = []

        except Exception as exc:
            error_count += 1
            print(
                f"[{index}/{len(records)}] AI processing failed for "
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
        f"{qualified_count} qualified, "
        f"{rejected_count} rejected, "
        f"{error_count} errors.",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    started_at = datetime.now(timezone.utc)

    print(
        f"Starting BruceTech incremental lead pipeline at "
        f"{started_at.isoformat()}...",
        flush=True,
    )

    # 1. Pull the latest Apify dataset.
    apify_posts = fetch_latest_apify_posts()

    # 2. Import only Facebook post URLs that are not already in Airtable.
    existing_urls = fetch_existing_facebook_urls()
    new_posts = select_new_posts(apify_posts, existing_urls)
    created_record_ids = create_new_posts_in_airtable(new_posts)

    if created_record_ids:
        print(
            "Waiting for Airtable formulas to calculate on new records...",
            flush=True,
        )
        time.sleep(AIRTABLE_FORMULA_WAIT_SECONDS)
    else:
        print(
            "No new Facebook posts were imported.",
            flush=True,
        )

    # 3. Process the unprocessed AI backlog, including older records.
    #
    # Only records matching all of the following are fetched:
    # - Prequalification = Send to AI
    # - AI Status is blank or Pending
    # - Text and Url are not empty
    #
    # Processed and Error records are excluded, so completed records are
    # never sent to OpenAI again.
    ai_records = fetch_ai_queue()
    process_ai_queue(ai_records)

    completed_at = datetime.now(timezone.utc)
    duration = (completed_at - started_at).total_seconds()

    print(
        f"BruceTech incremental pipeline completed at "
        f"{completed_at.isoformat()} in {duration:.1f}s.",
        flush=True,
    )


if __name__ == "__main__":
    main()