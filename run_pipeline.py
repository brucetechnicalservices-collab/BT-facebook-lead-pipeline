from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any
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
QUALIFICATION_THRESHOLD = int(
    os.getenv("QUALIFICATION_THRESHOLD", "55")
)

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

BruceTech is a Toronto-based web development, managed IT,
Microsoft 365, cybersecurity, AI consulting and business
automation company.

BruceTech services include:

Website and e-commerce:
- Website design and development
- Website redesign
- WordPress, Shopify and WooCommerce support
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
- SharePoint, OneDrive and Teams
- Device support
- Network and Wi-Fi
- VPN
- Backups
- Cybersecurity
- User onboarding and permissions

AI and automation:
- AI automation consultation
- AI readiness assessment
- Workflow automation
- CRM implementation and automation
- Airtable, Make and Zapier workflows
- API and system integrations
- Lead qualification and follow-up automation
- Reporting and data-entry automation
- Document and order-processing automation
- AI chatbots and internal assistants
- Business-process optimization

Analyze only the supplied public Facebook post. Do not invent facts.

A qualified lead must:
- Explicitly or strongly imply that help is needed
- Have credible business or organizational context
- Match at least one BruceTech service
- Appear unresolved
- Be appropriate for respectful outreach

Reject:
- Businesses advertising their own services
- Freelancers or agencies promoting themselves
- Job seekers
- Students and educational questions
- Personal consumer requests
- Spam
- General discussions without buying intent
- Requests already resolved
- Posts unrelated to BruceTech services

Scoring:
- Explicit request for help or provider: +30
- Clear BruceTech service match: +25
- Recent and actionable: +15
- Identifiable business or organization: +10
- Toronto, GTA, Ontario or Canada relevance: +10
- Urgency or business impact: +10
- DIY or educational discussion: -25
- Competitor, agency, job seeker or promotional post: -40
- Resolved request: -40
- No credible business context: -25
- Unrelated request: -40

A score of 55 or higher may be qualified.

For qualified leads, write a natural Facebook DM under 80 words.
Mention only supported details, include brucetech.ca naturally, and
end with one simple question.

For rejected posts, suggested_dm must be an empty string.

Always return all fields.
"""


LEAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "qualified": {
            "type": "boolean"
        },
        "lead_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100
        },
        "service_match": {
            "type": "string"
        },
        "lead_summary": {
            "type": "string"
        },
        "rejection_reason": {
            "type": "string"
        },
        "suggested_dm": {
            "type": "string"
        },
        "recommended_channel": {
            "type": "string",
            "enum": [
                "direct_message",
                "public_reply_then_dm",
                "do_not_contact"
            ]
        },
        "evidence": {
            "type": "string"
        }
    },
    "required": [
        "qualified",
        "lead_score",
        "service_match",
        "lead_summary",
        "rejection_reason",
        "suggested_dm",
        "recommended_channel",
        "evidence"
    ],
    "additionalProperties": False
}


def request_with_retry(
    method: str,
    url: str,
    *,
    max_attempts: int = 5,
    **kwargs: Any,
) -> requests.Response:
    for attempt in range(1, max_attempts + 1):
        response = requests.request(
            method,
            url,
            timeout=120,
            **kwargs,
        )

        if response.status_code < 400:
            return response

        if response.status_code in {429, 500, 502, 503, 504}:
            wait_seconds = min(2 ** attempt, 30)
            print(
                f"Request failed with {response.status_code}. "
                f"Retrying in {wait_seconds}s..."
            )
            time.sleep(wait_seconds)
            continue

        raise RuntimeError(
            f"Request failed: {response.status_code} "
            f"{response.text}"
        )

    raise RuntimeError(
        f"Request failed after {max_attempts} attempts: {url}"
    )


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
        f"{len(posts)} valid Facebook posts."
    )
    return posts


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [
        items[index:index + size]
        for index in range(0, len(items), size)
    ]


def map_apify_to_airtable(
    post: dict[str, Any]
) -> dict[str, Any]:
    user = post.get("user") or {}

    return {
        "Url": post.get("url", ""),
        "Facebook url": post.get("facebookUrl", ""),
        "Time": post.get("time"),
        "User ID": str(user.get("id", "")),
        "User name": user.get("name", ""),
        "Text": post.get("text", ""),
        "Group title": post.get("groupTitle", ""),
        "Input Url": post.get("inputUrl", ""),
        "Likes count": post.get("likesCount", 0) or 0,
        "Comments count": post.get("commentsCount", 0) or 0,
        "Shares count": post.get("sharesCount", 0) or 0,
    }


def upsert_posts_to_airtable(
    posts: list[dict[str, Any]]
) -> None:
    mapped = [
        {
            "fields": map_apify_to_airtable(post)
        }
        for post in posts
    ]

    for batch in chunks(mapped, 10):
        payload = {
            "performUpsert": {
                "fieldsToMergeOn": ["Url"]
            },
            "records": batch,
            "typecast": True,
        }

        response = request_with_retry(
            "PATCH",
            AIRTABLE_URL,
            headers=AIRTABLE_HEADERS,
            json=payload,
        )

        result = response.json()
        created = len(result.get("createdRecords", []))
        updated = len(result.get("updatedRecords", []))

        print(
            f"Airtable import: {created} created, "
            f"{updated} updated."
        )

        time.sleep(0.25)

def main() -> None:
    print("Starting BruceTech lead pipeline...", flush=True)

    posts = fetch_latest_apify_posts()
    print(f"Fetched {len(posts)} valid posts from Apify.", flush=True)

    upsert_posts_to_airtable(posts)

    print("Waiting for Airtable formulas to calculate...", flush=True)
    time.sleep(10)

    ai_records = fetch_ai_queue()
    print(f"Found {len(ai_records)} AI candidates.", flush=True)

    process_ai_queue(ai_records)

    print("BruceTech lead pipeline completed.", flush=True)


if __name__ == "__main__":
    main()
