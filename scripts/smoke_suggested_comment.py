"""
One-off REAL OpenAI smoke test for Suggested Comment.

Scratch harness. NOT part of the repository, not committed, and it modifies
nothing. It imports the production functions from the branch as they are.

WHAT IT TOUCHES
    OpenAI   yes, real gpt-5-mini, one call per post (5 total)
    Airtable no. No Airtable function is called or imported for I/O.
    Apify    no.
    Records  no. Every record is built in memory here.

The only thing this file supplies is the post text and a fresh timestamp.
Everything else -- the prompt, the JSON schema, the retry policy, the
qualification rules, the outreach gate, and sanitize_outreach_copy -- is the
production code path from run_pipeline.qualify_post.

USAGE
    cd /path/to/BT-facebook-lead-pipeline
    OPENAI_API_KEY=sk-... python scripts/smoke_suggested_comment.py

Cost: 5 gpt-5-mini calls.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("BT_REPO", os.getcwd())
sys.path.insert(0, REPO)

# NOTE: the OPENAI_API_KEY guard lives inside main(), not here. A module-level
# sys.exit() fires during pytest collection and takes the whole test job down
# with it, which is exactly what happened on the first attempt at this run.
# The filename also deliberately avoids test_*.py and *_test.py so pytest does
# not collect this file at all.

# Production defaults already match the workflow (threshold 65, manual review
# 55, hot 80, max age 5, confidence 0.55, business pain 70, gpt-5-mini), so
# nothing is overridden here. Set them explicitly only if your shell differs.
import run_pipeline as rp                      # noqa: E402
from qualification import prefilter_post       # noqa: E402

NOW = datetime.now(timezone.utc)
FRESH = (NOW - timedelta(days=1)).isoformat()

EM_DASH = "—"
URL_RE = re.compile(r"https?://|www\.|\.com\b|\.ca\b", re.IGNORECASE)
PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")
PRICE_RE = re.compile(r"[$£€]\s?\d|\b\d+\s?(?:k|dollars|per month|/mo)\b", re.I)
DM_RE = re.compile(r"\bdm\b|\bmessage you\b|\bsend you\b|\bshoot you\b|\breach out\b", re.I)

POSTS = [
    ("A lead-gen / business pain", "Matt House", """Bit of a read but looking for some guidance Currently own a business in a capital intensive, low margin industry with long term focus in wealth. Looking to take the next step and launch a service based business covering a variety of Surface Restoration and Installation niches.

Capital is accessible for equipment and insurance, trucks, etc.

I know what we need to sell in terms of volume per industry, but physically selling jobs is new to me.

Am I looking at hiring a sales manager and investing into some lead generation?

To summarize, both of us possess the ability to physically get this business off the ground and launch it as far of job execution goes, but neither of us are in a position to properly market and sell to our target customer base.

Were leaning towards paid lead generation and quickly adding a sales agent to manage that side, but again, this is foreign to us."""),

    ("B microsoft 365 intake", "", """Customer intake and service requests

As a small electrical contractor, most of my revenue comes from service work. I am looking at ways to systemize my customer and job intake.

I know a lot of people utilize CRMs such as ServiceTitan, Jobber, Housecall.

But being that I already have access to Microsoft 365, I am considering setting up some workflows and forms through that.

Is there a more streamline/professional way to handle all of this than sending a calling customer a customer information form for them to fill out?

Ultimately I want to make things as simple and efficient as possible for the customer."""),

    ("C AI answering service", "Denise Perez", """I am looking for an AI answering service. Someone that can answer the phone and book after hours or while I'm with patients. Who do you recommend?"""),

    ("D gohighlevel", "Oren Gold", """I need help with GoHighLevel. Any recommendations? I need an expert."""),

    ("NEG pool tool research", "", """We're launching our own dedicated pool division here in South Texas.

What apps, software, or websites do y'all recommend for pool design, estimating and job costing, scheduling and project management, customer proposals and presentations?

I've been looking into programs like Pool Studio/Vip3D and JobTread."""),
]


def yn(value: bool) -> str:
    return "yes" if value else "no"


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. This test makes real model calls.")

    calls = 0

    for name, author, text in POSTS:
        fields = {
            rp.FIELD_TEXT: text,
            rp.FIELD_USER_NAME: author,
            rp.FIELD_TIME: FRESH,
            rp.FIELD_URL: "https://www.facebook.com/groups/1/posts/smoketest",
            rp.FIELD_GROUP_TITLE: "Smoke Test",
            rp.FIELD_COMMENTS: 3,
        }

        prefilter = prefilter_post(
            text,
            post_time=FRESH,
            now=NOW,
            max_post_age_days=rp.MAX_POST_AGE_DAYS,
            comment_count=3,
            pass_score=rp.PREFILTER_PASS_SCORE,
            allow_tool_research=rp.ALLOW_TOOL_RESEARCH_TO_AI,
        )

        print("=" * 78)
        print(name)
        print("=" * 78)
        print(f"  intent_type      : {prefilter.intent_type}")
        print(f"  prefilter score  : {prefilter.score}")
        print(f"  match basis      : {prefilter.match_basis}")
        print(f"  reaches model    : {prefilter.passed}")

        if not prefilter.passed:
            print("  SKIPPED: blocked before the AI, no call made.")
            print(f"  codes            : {prefilter.rejection_codes}")
            continue

        # THE REAL CALL. Production prompt, schema, retries, gate, sanitiser.
        decision, signals = rp.qualify_post(fields, prefilter=prefilter, now=NOW)
        calls += 1

        payload = rp.map_decision_to_airtable(
            decision, signals, prefilter_score=prefilter.score, prefilter=prefilter
        )

        raw_dm = signals.get("suggested_dm", "")
        raw_comment = signals.get("suggested_comment", "")
        final_dm = payload[rp.FIELD_SUGGESTED_DM]
        final_comment = payload[rp.FIELD_SUGGESTED_COMMENT]

        print(f"  lead score       : {payload[rp.FIELD_LEAD_SCORE]}")
        print(f"  Qualified        : {payload[rp.FIELD_QUALIFIED]}")
        print(f"  Manual Review    : {payload[rp.FIELD_MANUAL_REVIEW]}")
        print(f"  Lead Tier        : {payload[rp.FIELD_LEAD_TIER]}")
        print(f"  Outreach Ready   : {payload[rp.FIELD_OUTREACH_READY]}")
        print(f"  Recommended chan : {payload[rp.FIELD_RECOMMENDED_CHANNEL]}")
        print(f"  Disqualifiers    : {payload[rp.FIELD_DISQUALIFIERS] or '(none)'}")
        print()
        print(f"  RAW model suggested_dm      : {raw_dm!r}")
        print(f"  RAW model suggested_comment : {raw_comment!r}")
        print()
        print(f"  FINAL Suggested DM      : {final_dm!r}")
        print(f"  FINAL Suggested Comment : {final_comment!r}")
        print()

        words = len(final_comment.split())
        sentences = len([s for s in re.split(r"[.!?]+", final_comment) if s.strip()])
        print(f"  comment word count : {words}")
        print(f"  comment sentences  : {sentences}")
        print(f"  URL present        : {yn(bool(URL_RE.search(final_comment)))}")
        print(f"  brucetech.ca       : {yn('brucetech.ca' in final_comment.lower())}")
        print(f"  phone present      : {yn(bool(PHONE_RE.search(final_comment)))}")
        print(f"  pricing present    : {yn(bool(PRICE_RE.search(final_comment)))}")
        print(f"  em dash in comment : {yn(EM_DASH in final_comment)}")
        print(f"  em dash in DM      : {yn(EM_DASH in final_dm)}")
        print(f"  DM transition      : {yn(bool(DM_RE.search(final_comment)))}")
        print(f"  em dash in RAW dm  : {yn(EM_DASH in str(raw_dm))}")
        print(f"  em dash in RAW cmt : {yn(EM_DASH in str(raw_comment))}")
        print()

    print("=" * 78)
    print(f"REAL OPENAI CALLS: {calls}")
    print("Airtable writes: 0 (no Airtable I/O function was called)")
    print("Apify runs: 0")


if __name__ == "__main__":
    main()
