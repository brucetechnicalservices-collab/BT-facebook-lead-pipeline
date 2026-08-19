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

The 2026-08-19 medical spa fixtures carry their own contract:

================  ================  ==============  ==================
Fixture           Intent            Match basis     Outcome
================  ================  ==============  ==================
MEDSPA_INVENTORY  BUSINESS_PAIN     described       reaches AI
MEDSPA_ESTHETIC.  BUSINESS_PAIN     described       reaches AI
SOLO_MD_MARKET.   PROVIDER_REQUEST  adjacent        reaches AI
NEW_MEDSPA_MARK.  PROVIDER_REQUEST  adjacent        reaches AI
MANGOMINT_OR_BLV  TOOL_RESEARCH     named           research only
BOULEVARD_SWITCH  TOOL_RESEARCH     none            never reaches AI
WEB_DESIGN_CTA    (promotional)     n/a             never reaches AI
MEDSPA_NOISE_*    various           none            never reaches AI
================  ================  ==============  ==================
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
# The 2026-08-19 fresh-run medical spa scrape (Apify run Q3Ix6zmHrEDhgiQGf).
#
# The infrastructure worked: 50 posts, correctly attributed. Qualification
# recall did not -- effectively nothing reached the model, including several
# credible business problems and provider requests. These are the posts that
# exposed it, lightly sanitised.
#
# The first six must reach the model. The rest must not, and are the reason
# the recall fixes are narrow: a medical spa group talks about buying lasers
# and syringes all day, and none of that is BruceTech work.
# ---------------------------------------------------------------------------

#: Operational systems failure with no software vocabulary anywhere in it.
#: Previously UNRELATED with no service match.
MEDSPA_INVENTORY_NO_SYSTEMS = (
    "Hi I'm a new manager at a med spa and we have been having a issue with "
    "inventory. We are missing about 200 units and nobody can tell me where "
    "they went. I came into a business that is bleeding and no systems in "
    "place and starting from the ground up and was trying to see the "
    "different ways I could go about it."
)

#: "We offer" as context for a problem, not as an advertisement. Previously
#: rejected as PROMOTIONAL_POST on those two words alone.
MEDSPA_ESTHETICIAN_SCHEDULE = (
    "Med spa owner here! What are some ways that helped fill up your "
    "estheticians schedule? She is newer and we struggle to keep her busy. "
    "We offer facials, DiamondGlow, SkinPen and a few other treatments but "
    "her books are half empty most weeks."
)

#: The same words with a call to action. This one really is promotional.
WEB_DESIGN_SELLER_CTA = (
    "We offer web design and SEO for clinics across the GTA. Book a call "
    "with me this week and I'll audit your site for free. Limited time."
)

#: Marketing provider request with a measurable acquisition goal.
SOLO_MD_MARKETING_AGENCY = (
    "I'm a solo MD looking for marketing agency or platform to get me "
    "patients. Anyone have someone they actually trust? Happy to pay for "
    "something that works."
)

NEW_MEDSPA_MARKETING_COMPANY = (
    "I recently opened my own med spa and I am looking for a reliable "
    "marketing company that actually delivers real results and brings in "
    "real clients. Tired of paying for promises."
)

#: Software either/or over a named category. Previously UNRELATED.
MANGOMINT_OR_BOULEVARD = (
    "Mangomint or boulevard POS system and why? Trying to decide before we "
    "open next month and I keep going back and forth on it."
)

BOULEVARD_SWITCHING_RESEARCH = (
    "We currently use Boulevard and are thinking of switching to either "
    "Mangomint or GlossGenius. Pros and cons? Curious what made people move."
)

# --- Must stay rejected before the AI ---------------------------------------

#: Creative work only. A real provider request with no BruceTech fit.
INFLUENCER_ONLY_REQUEST = (
    "Looking for an influencer to promote our new facial line, and maybe a "
    "photographer for content. Just brand awareness stuff for now."
)

INJECTOR_HIRING = (
    "We are hiring an experienced injector for our clinic. Competitive pay, "
    "must have 2 years experience with neurotoxins and filler. Send resume."
)

AESTHETIC_CHAIR_FOR_SALE = (
    "Aesthetic treatment chair for sale, barely used, $1800 obo. Pick up "
    "only, message me if interested."
)

#: Equipment shopping. Contains "platform" and "clinic" and is not our work.
LASER_DEVICE_PURCHASE = (
    "Looking at buying a laser device for hair removal. Anyone have "
    "experience with the Candela vs Cynosure platforms for a small clinic?"
)

BOTOX_SYRINGE_PREFERENCE = (
    "What syringe do you all prefer for botox injections? I've been using a "
    "31g and wondering if there is something better out there."
)

SKINCARE_INGREDIENT_CHAT = (
    "What's one skincare ingredient you think is seriously underrated? I "
    "feel like niacinamide never gets the credit it deserves honestly."
)

HUNDRED_K_DEVICE_QUESTION = (
    "If you had $100k to spend on one device, what would you buy and why? "
    "Curious what everyone would pick if they were starting over."
)

TATTOO_REMOVAL_EQUIPMENT = (
    "Tattoo removal equipment comparisons, anyone used the PicoWay vs "
    "PicoSure? Trying to figure out which machine performs better."
)

CPA_SCORP_PROMOTION = (
    "Most med spa owners overpay taxes. As a CPA I help clinics elect "
    "S-Corp status and save thousands. DM me for a free consultation to "
    "review your books."
)

NURSE_PRACTITIONER_JOB_SEEKER = (
    "Nurse practitioner looking for work in the aesthetics space. 6 years "
    "experience with injectables, resume attached, available for hire "
    "immediately."
)

#: Every fresh-run post that must never reach the model, in one place.
MEDSPA_NOISE_FIXTURES = {
    "INFLUENCER_ONLY_REQUEST": INFLUENCER_ONLY_REQUEST,
    "INJECTOR_HIRING": INJECTOR_HIRING,
    "AESTHETIC_CHAIR_FOR_SALE": AESTHETIC_CHAIR_FOR_SALE,
    "LASER_DEVICE_PURCHASE": LASER_DEVICE_PURCHASE,
    "BOTOX_SYRINGE_PREFERENCE": BOTOX_SYRINGE_PREFERENCE,
    "SKINCARE_INGREDIENT_CHAT": SKINCARE_INGREDIENT_CHAT,
    "HUNDRED_K_DEVICE_QUESTION": HUNDRED_K_DEVICE_QUESTION,
    "TATTOO_REMOVAL_EQUIPMENT": TATTOO_REMOVAL_EQUIPMENT,
    "CPA_SCORP_PROMOTION": CPA_SCORP_PROMOTION,
    "NURSE_PRACTITIONER_JOB_SEEKER": NURSE_PRACTITIONER_JOB_SEEKER,
}

#: Every fresh-run post that must reach the model.
MEDSPA_CANDIDATE_FIXTURES = {
    "MEDSPA_INVENTORY_NO_SYSTEMS": MEDSPA_INVENTORY_NO_SYSTEMS,
    "MEDSPA_ESTHETICIAN_SCHEDULE": MEDSPA_ESTHETICIAN_SCHEDULE,
    "SOLO_MD_MARKETING_AGENCY": SOLO_MD_MARKETING_AGENCY,
    "NEW_MEDSPA_MARKETING_COMPANY": NEW_MEDSPA_MARKETING_COMPANY,
}


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
    **MEDSPA_CANDIDATE_FIXTURES,
    "MANGOMINT_OR_BOULEVARD": MANGOMINT_OR_BOULEVARD,
    "BOULEVARD_SWITCHING_RESEARCH": BOULEVARD_SWITCHING_RESEARCH,
    "WEB_DESIGN_SELLER_CTA": WEB_DESIGN_SELLER_CTA,
    **MEDSPA_NOISE_FIXTURES,
    "PROMOTIONAL_AGENCY": PROMOTIONAL_AGENCY,
    "JOB_SEEKER": JOB_SEEKER,
    "API_INTEGRATION": API_INTEGRATION,
    "OFFICE_NETWORK": OFFICE_NETWORK,
    "RESOLVED_REQUEST": RESOLVED_REQUEST,
}
