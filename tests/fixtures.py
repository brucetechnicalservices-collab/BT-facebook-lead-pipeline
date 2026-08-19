"""
Sanitised fixtures reproducing real production failure patterns.

Each one is a post shape the pipeline previously got wrong, or a shape it must
keep getting right. They are sanitised — no real names, URLs, or business
identifiers — but the language and structure mirror what the scraper returns.

The expectations encoded here are the contract:

============  ====================  =============  ==================
Fixture       Intent                Service match  Outcome
============  ====================  =============  ==================
EXCAVATION    GENERAL_ADVICE        no             never reaches AI
GOHIGHLEVEL   PROVIDER_REQUEST      yes            outreach candidate
PEN_AND_PAPER BUSINESS_PAIN         yes            AI, then evidence
VIRTUAL_ASST  UNRELATED             no             never reaches AI
CRM_RESEARCH  TOOL_RESEARCH         yes            manual review only
AIRBNB_TENANCY TOOL_RESEARCH        no             never reaches AI
WEBSITE_WORTH UNRELATED             yes            AI only if approved
============  ====================  =============  ==================
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Example A -- the regression that started this release.
#
# "working capital" contains the substring "api". The old prefilter matched
# service keywords with `"api" in text`, so an excavation-financing question
# registered as an API integration lead and was sent to the model.
# ---------------------------------------------------------------------------

EXCAVATION_FUNDING = (
    "I own an excavation company and need business funding for another "
    "truck, another machine, and working capital. Anyone been through this "
    "recently and know what the banks are looking for?"
)

# ---------------------------------------------------------------------------
# Example B -- the shape of a genuinely strong lead.
# ---------------------------------------------------------------------------

GOHIGHLEVEL_PROVIDER = (
    "Looking for a company that's done this well for an HVAC business. "
    "Need the whole GoHighLevel setup with SMS sequences, email drips and "
    "social integration. We have about 20 techs and our follow-up is a mess "
    "right now. Happy to pay properly for someone who knows the platform."
)

# ---------------------------------------------------------------------------
# Example C -- business pain. A real opportunity, but the author has not
# asked for a provider, so outreach needs a higher bar.
# ---------------------------------------------------------------------------

PEN_AND_PAPER_ELECTRICIAN = (
    "I run an electrical company and still use pen and paper to schedule "
    "jobs. I'm answering calls and texts all evening and need a better way "
    "to manage it. Half the time I forget to call people back the next day."
)

# ---------------------------------------------------------------------------
# Example D -- solution hopping. A real provider request for something
# BruceTech does not sell. The old pipeline would reason "we could automate
# this instead" and treat it as a lead.
# ---------------------------------------------------------------------------

VIRTUAL_ASSISTANT = (
    "Looking for a reliable virtual assistant to help me keep on top of my "
    "emails and calendar each week. Around 10 hours a week to start, "
    "references appreciated."
)

# ---------------------------------------------------------------------------
# Example E -- tool research. Names a BruceTech product area, but is asking
# which product to buy, not who should implement it.
# ---------------------------------------------------------------------------

CRM_RESEARCH = (
    "What CRM are other contractors using? Curious what everyone likes for "
    "tracking jobs and quotes these days, there seem to be a hundred "
    "options and I can't tell them apart."
)

# ---------------------------------------------------------------------------
# Example F -- solution hopping, taken from the 2026-08-18 production run.
#
# A short-term rental host asking about lease contracts, tenancy rules,
# background checks, and booking risk. The model scored it 77 and proposed AI
# automation, workflow automation, and a CRM: none of which the author asked
# for, and none of which answers a tenancy-law question.
#
# The request is legal and administrative advice. BruceTech being able to
# automate part of somebody's week is not a reason to treat their question as
# a lead. This must not qualify, and must not produce a DM.
# ---------------------------------------------------------------------------

AIRBNB_TENANCY_ADVICE = (
    "Hi all, I host a few short term rental units and I'm thinking about "
    "switching one over to a long term rental. What are people using for the "
    "lease contract, and are there tenancy rules I should know about before "
    "I sign anything? Also curious how everyone handles background checks on "
    "tenants, and whether the booking risk of losing the short term income is "
    "worth it. Any advice appreciated."
)

# ---------------------------------------------------------------------------
# Example G -- the one thing a human Approve is allowed to change.
#
# General chatter that names real BruceTech services. The intent heuristic
# vetoes it and it never reaches the model on its own. A reviewer who reads it
# and sets Human Decision = Approve can send it to the model anyway, because
# the only thing standing in the way is a heuristic guess about intent.
#
# Its rejection codes are overridable *by construction*: no STALE_POST, no
# NO_SERVICE_MATCH, no promotional or resolved signal.
# ---------------------------------------------------------------------------

WEBSITE_WORTH_IT_ADVICE = (
    "Curious what everyone thinks. Is it still worth having a website for a "
    "small landscaping business in 2026, or is a Facebook page enough these "
    "days? Wondering if SEO actually brings in any work."
)

# ---------------------------------------------------------------------------
# Supporting fixtures
# ---------------------------------------------------------------------------

#: A provider advertising itself. Must never reach the AI.
PROMOTIONAL_AGENCY = (
    "I offer website design and SEO services for small businesses across "
    "the GTA. DM me for a free consultation, limited time discount on "
    "WordPress builds this month!"
)

#: Someone looking for work rather than looking to buy.
JOB_SEEKER = (
    "Experienced WordPress developer looking for work in the Toronto area. "
    "10 years building websites and Shopify stores, resume attached, "
    "available for hire immediately."
)

#: An explicit API integration request. "api" must still match here.
API_INTEGRATION = (
    "Need help integrating an API between our booking system and our "
    "accounting software. Small job but it has to be done properly."
)

#: A managed-IT request carried by weak service terms plus technical context.
OFFICE_NETWORK = (
    "Our office network keeps dropping and nobody can print. Need this "
    "fixed properly, we've been limping along for a month now."
)

#: A request that has already been answered.
RESOLVED_REQUEST = (
    "Looking for a web developer to rebuild our WordPress site with online "
    "booking. EDIT: found someone, thanks everyone for the recommendations!"
)

ALL_FIXTURES = {
    "EXCAVATION_FUNDING": EXCAVATION_FUNDING,
    "GOHIGHLEVEL_PROVIDER": GOHIGHLEVEL_PROVIDER,
    "PEN_AND_PAPER_ELECTRICIAN": PEN_AND_PAPER_ELECTRICIAN,
    "VIRTUAL_ASSISTANT": VIRTUAL_ASSISTANT,
    "CRM_RESEARCH": CRM_RESEARCH,
    "AIRBNB_TENANCY_ADVICE": AIRBNB_TENANCY_ADVICE,
    "WEBSITE_WORTH_IT_ADVICE": WEBSITE_WORTH_IT_ADVICE,
    "PROMOTIONAL_AGENCY": PROMOTIONAL_AGENCY,
    "JOB_SEEKER": JOB_SEEKER,
    "API_INTEGRATION": API_INTEGRATION,
    "OFFICE_NETWORK": OFFICE_NETWORK,
    "RESOLVED_REQUEST": RESOLVED_REQUEST,
}
