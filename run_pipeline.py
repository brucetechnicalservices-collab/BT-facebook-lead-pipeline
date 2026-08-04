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


APIFY_TOKEN = os.environ["APIFY_TOKEN"]
APIFY_TASK_ID = os.environ["APIFY_TASK_ID"]
AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_TABLE_NAME = os.environ["AIRTABLE_TABLE_NAME"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
AI_BATCH_LIMIT = int(os.getenv("AI_BATCH_LIMIT", "100"))
QUALIFICATION_THRESHOLD = int(os.getenv("QUALIFICATION_THRESHOLD", "55"))
AIRTABLE_FORMULA_WAIT_SECONDS = int(os.getenv("AIRTABLE_FORMULA_WAIT_SECONDS", "15"))

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

SYSTEM_INSTRUCTIONS = """
You are BruceTech's Facebook lead qualification analyst.

BruceTech is a Toronto-based web development, managed IT, Microsoft 365,
cybersecurity, AI consulting, and business automation company.

BruceTech services include website design, redesign, WordPress, Shopify,
WooCommerce, booking and payment integrations, SEO, business email, managed IT,
Microsoft 365, networks, Wi-Fi, VPN, backups, cybersecurity, CRM, AI automation
consultation, workflow automation, process mapping, API integrations, lead and
customer follow-up automation, reporting, document processing, order processing,
AI chatbots, AI receptionists, internal assistants, and custom AI workflows.

Analyze only the supplied public Facebook post and metadata. Do not invent facts.

A qualified lead must strongly imply that help is needed, have credible business
or organizational context, match a BruceTech service, appear unresolved, and be
appropriate for respectful outreach.

Posts may show buying intent indirectly by asking what software, CRM, POS,
booking, payment, or website tool to use; asking for recommendations; describing
a repetitive manual process; switching systems; asking how to integrate systems;
or reporting missed calls, follow-up, booking, payments, website, or IT problems.

Reject businesses advertising themselves, freelancers or agencies promoting
themselves, job seekers, students, personal consumer requests, spam, general
discussions without meaningful buying intent, resolved requests, unrelated
requests, and requests only for free work.

Scoring:
- Explicit request for help, recommendation, or provider: +30
- Clear BruceTech service match: +25
- Recent and actionable: +15
- Identifiable business or organization: +10
- Toronto, GTA, Ontario, or Canada relevance: +10
- Urgency or meaningful business impact: +10
- DIY or educational discussion: -25
- Competitor, agency, job seeker, or promotional post: -40
- Resolved request: -40
- No credible business context: -25
- Unrelated request: -40

A score of 55 or higher may be qualified. Do not force the threshold.

For qualified leads, write a natural Facebook DM under 80 words. Mention only
supported details, include brucetech.ca naturally, and end with one simple
question. Never mention scraping, monitoring, tracking, or AI analysis.

For rejected posts, suggested_dm must be empty and recommended_channel must be
do_not_contact. Always return every structured-output field. Use "None" when
service_match or rejection_reason does not apply.
""".strip()

LEAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "qualified": {"type": "boolean"},
        "lead_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "service_match": {"type": "string"},
        "lead_summary": {"type": "string"},
        "rejection_reason": {"type": "string"},
        "suggested_dm": {"type": "string"},
        "recommended_channel": {
            "type": "string",
            "enum": ["direct_message", "public_reply_then_dm", "do_not_contact"],
        },
        "evidence": {"type": "string"},
    },
    "required": [
        "qualified", "lead_score", "service_match", "lead_summary",
        "rejection_reason", "suggested_dm", "recommended_channel", "evidence"
    ],
    "additionalProperties": False,
}


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
            print(f"Network error: {exc}. Retrying in {wait_seconds}s...", flush=True)
            time.sleep(wait_seconds)
            continue

        if response.status_code < 400:
            return response

        if response.status_code in {429, 500, 502, 503, 504}:
            if attempt == max_attempts:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            wait_seconds = int(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 30)
            print(f"Request failed with {response.status_code}. Retrying in {wait_seconds}s...", flush=True)
            time.sleep(wait_seconds)
            continue

        raise RuntimeError(f"Request failed: {response.status_code} {response.text}")

    raise RuntimeError(
        f"Request failed after {max_attempts} attempts: {url}. Last error: {last_error}"
    )


def fetch_latest_apify_posts() -> list[dict[str, Any]]:
    url = f"https://api.apify.com/v2/actor-tasks/{APIFY_TASK_ID}/runs/last/dataset/items"
    params = {
        "token": APIFY_TOKEN,
        "status": "SUCCEEDED",
        "format": "json",
        "clean": "true",
        "fields": (
            "legacyId,url,time,text,user,groupTitle,facebookUrl,inputUrl,"
            "likesCount,commentsCount,sharesCount,error,errorDescription"
        ),
    }
    response = request_with_retry("GET", url, params=params)
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("Apify returned an unexpected response instead of a list.")
    posts = [
        item for item in data
        if item.get("url") and item.get("legacyId") and not item.get("error")
    ]
    print(f"Fetched {len(data)} Apify items; {len(posts)} valid Facebook posts.", flush=True)
    return posts


def map_apify_to_airtable(post: dict[str, Any]) -> dict[str, Any]:
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


def upsert_posts_to_airtable(posts: list[dict[str, Any]]) -> tuple[int, int]:
    if not posts:
        print("No valid Apify posts to import.", flush=True)
        return 0, 0
    mapped = [{"fields": map_apify_to_airtable(post)} for post in posts]
    total_created = 0
    total_updated = 0
    for batch_number, batch in enumerate(chunks(mapped, 10), start=1):
        payload = {
            "performUpsert": {"fieldsToMergeOn": [FIELD_URL]},
            "records": batch,
            "typecast": True,
        }
        response = request_with_retry(
            "PATCH", AIRTABLE_URL, headers=AIRTABLE_HEADERS, json=payload
        )
        result = response.json()
        created = len(result.get("createdRecords", []))
        updated = len(result.get("updatedRecords", []))
        total_created += created
        total_updated += updated
        print(
            f"Airtable batch {batch_number}: {created} created, {updated} updated.",
            flush=True,
        )
        time.sleep(0.25)
    print(
        f"Airtable import complete: {total_created} created, {total_updated} updated.",
        flush=True,
    )
    return total_created, total_updated


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
            "GET", AIRTABLE_URL, headers=AIRTABLE_HEADERS, params=params
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


def fetch_ai_queue() -> list[dict[str, Any]]:
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
        f"Found {len(records)} records in the AI queue (limit: {AI_BATCH_LIMIT}).",
        flush=True,
    )
    return records


def qualify_post(fields: dict[str, Any]) -> dict[str, Any]:
    post_input = f"""
Evaluate this Facebook post as a potential BruceTech business lead.

POST AUTHOR:
{fields.get(FIELD_USER_NAME, "")}

POST DATE:
{fields.get(FIELD_TIME, "")}

FACEBOOK GROUP:
{fields.get(FIELD_GROUP_TITLE, "")}

POST TEXT:
{fields.get(FIELD_TEXT, "")}

POST URL:
{fields.get(FIELD_URL, "")}
""".strip()
    response = OPENAI_CLIENT.responses.create(
        model=OPENAI_MODEL,
        instructions=SYSTEM_INSTRUCTIONS,
        input=post_input,
        text={
            "format": {
                "type": "json_schema",
                "name": "brucetech_lead_result",
                "schema": LEAD_SCHEMA,
                "strict": True,
            }
        },
        max_output_tokens=1000,
    )
    if not response.output_text:
        raise RuntimeError("OpenAI returned an empty response.")
    result = json.loads(response.output_text)
    score = int(result.get("lead_score", 0))
    result["lead_score"] = max(0, min(score, 100))
    if result["lead_score"] < QUALIFICATION_THRESHOLD:
        result["qualified"] = False
    if not result.get("qualified", False):
        result["qualified"] = False
        result["suggested_dm"] = ""
        result["recommended_channel"] = "do_not_contact"
        result["service_match"] = result.get("service_match") or "None"
        result["rejection_reason"] = result.get("rejection_reason") or (
            "The post did not meet the qualification threshold."
        )
    else:
        result["rejection_reason"] = result.get("rejection_reason") or "None"
    return result


def map_ai_result_to_airtable(result: dict[str, Any]) -> dict[str, Any]:
    return {
        FIELD_AI_STATUS: "Processed",
        FIELD_QUALIFIED: bool(result["qualified"]),
        FIELD_LEAD_SCORE: int(result["lead_score"]),
        FIELD_SERVICE_MATCH: result.get("service_match") or "None",
        FIELD_LEAD_SUMMARY: result.get("lead_summary", ""),
        FIELD_REJECTION_REASON: result.get("rejection_reason") or "None",
        FIELD_SUGGESTED_DM: result.get("suggested_dm", ""),
        FIELD_RECOMMENDED_CHANNEL: result.get("recommended_channel", "do_not_contact"),
        FIELD_EVIDENCE: result.get("evidence", ""),
    }


def update_airtable_records(updates: list[dict[str, Any]]) -> None:
    if not updates:
        return
    for batch in chunks(updates, 10):
        request_with_retry(
            "PATCH",
            AIRTABLE_URL,
            headers=AIRTABLE_HEADERS,
            json={"records": batch, "typecast": True},
        )
        time.sleep(0.25)


def mark_ai_error(record_id: str, error_message: str) -> None:
    payload = {
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
    }
    request_with_retry(
        "PATCH", AIRTABLE_URL, headers=AIRTABLE_HEADERS, json=payload
    )


def process_ai_queue(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No records require OpenAI qualification.", flush=True)
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
                {"id": record_id, "fields": map_ai_result_to_airtable(result)}
            )
            print(
                f"[{index}/{len(records)}] Score={result['lead_score']} "
                f"Qualified={result['qualified']} Author={author!r} URL={post_url}",
                flush=True,
            )
            if len(pending_updates) == 10:
                update_airtable_records(pending_updates)
                pending_updates = []
        except Exception as exc:
            error_count += 1
            print(
                f"[{index}/{len(records)}] AI processing failed for {record_id}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            try:
                mark_ai_error(record_id, str(exc))
            except Exception as mark_exc:
                print(
                    f"Could not mark Airtable record {record_id} as Error: {mark_exc}",
                    file=sys.stderr,
                    flush=True,
                )
    update_airtable_records(pending_updates)
    print(
        f"AI processing complete: {qualified_count} qualified, "
        f"{rejected_count} rejected, {error_count} errors.",
        flush=True,
    )


def main() -> None:
    started_at = datetime.now(timezone.utc)
    print(
        f"Starting BruceTech lead pipeline at {started_at.isoformat()}...",
        flush=True,
    )
    posts = fetch_latest_apify_posts()
    upsert_posts_to_airtable(posts)
    print("Waiting for Airtable formulas to calculate...", flush=True)
    time.sleep(AIRTABLE_FORMULA_WAIT_SECONDS)
    ai_records = fetch_ai_queue()
    process_ai_queue(ai_records)
    completed_at = datetime.now(timezone.utc)
    duration = (completed_at - started_at).total_seconds()
    print(
        f"BruceTech lead pipeline completed at {completed_at.isoformat()} "
        f"in {duration:.1f}s.",
        flush=True,
    )


if __name__ == "__main__":
    main()