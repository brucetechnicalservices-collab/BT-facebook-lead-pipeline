# Apify source evaluation

An assessment of `simpleapi/facebook-groups-search-scraper` ("Facebook Groups
Search Scraper With Group Profile Details") against what this pipeline needs,
and what would serve the website analysis focus better.

---

## Verdict in one line

It is a **group discovery** tool, not a member-profile or post source. It
cannot feed this pipeline, but it is a genuinely useful way to decide which
groups the existing post scraper should be pointed at.

---

## What it actually returns

| Input | Output |
|---|---|
| Keywords (`bmw`, `tesla`) or direct group URLs | One row **per group** |

Per the listing, each row carries the group's name, member count, category,
link, snippet, description, discoverability, and numeric member/post stats —
harvested from the group's About page. Options are max groups per keyword
(default 100) and duplicate merging across keywords.

## What it does not return

**Member profiles.** The "Group Profile Details" in the name is the *group's*
profile, not the profiles of its members. Facebook does not expose group
member lists publicly, and nothing in the documented output schema is
per-person. Any actor that claims per-member data at scale is either reading
a logged-in session — against Facebook's terms, with real account-ban
exposure — or is quietly returning post authors instead.

**Posts.** This is the blocking problem for the pipeline. Every field this
repo consumes comes from a post:

```
legacyId · url · time · text · user · groupTitle · facebookUrl · inputUrl
likesCount · commentsCount · sharesCount
```

A group row supplies none of them. `Text` would be empty, so the prefilter
rejects everything, the AI has nothing to read, and dedup has no fingerprint.
Swapping the current task for this one produces a zero-lead run, not a
different one.

## Where it is worth money

Group discovery is currently a manual job: someone decides which group URLs
go in the Apify task's input list. This actor automates exactly that step.
Search `toronto small business`, `ontario entrepreneurs`, `gta restaurant
owners`, `shopify canada`, and get back a ranked list with member counts and
categories — which is the information you actually use to pick groups.

That is an occasional operations task, not a per-run pipeline stage. Run it
by hand, review the results, add the good groups to the post scraper's input
list. Nothing in this repo needs to change for that.

At $2.99 / 1,000 results a discovery sweep of a few hundred groups costs
about a dollar.

### Before relying on it

The listing shows **0 reviews and a 0.0 rating**, and the console shows no
run history. It is unproven, so evaluate it on its own before it influences
group selection: one small run, two or three keywords, and check whether the
member counts and categories match what you see when you open the groups.
Public search also skews toward large public groups, and Facebook's member
counts are approximate.

---

## A better fit for the website analysis focus

The focus mode added in this branch qualifies **posts** — people who have
already said something. That is a good funnel, but it only ever reaches
businesses that happen to post.

The stated target is "business owners and pages and their websites". For
that, a **Facebook Pages scraper** is the higher-signal source, because a
business page carries a `website` field:

- **Page with no website** → the "has customers but no site" offer, evidenced
  rather than guessed.
- **Page with a website** → fetch it and fingerprint the platform. Shopify,
  Wix, and Squarespace announce themselves in response headers and page
  markup, which identifies the monthly-fee conversion target directly. The
  same fetch shows a dated design, a broken or slow site, a missing SSL
  certificate, or no mobile viewport.

That inverts the funnel: instead of waiting for a business to complain about
its website, you qualify the website itself. It would be a second entry point
into the same Airtable base and the same qualification rules, feeding
`website_opportunity` and `website_platform` from observed facts rather than
from what a post happens to mention.

It is a separate build, not a change to this one. Noted here so the decision
is on the record rather than rediscovered later.

---

## Summary

| Question | Answer |
|---|---|
| Good for group member profiles? | No — it returns groups, not members. Public member profiles are not available at scale. |
| Better than the current scraper? | Not comparable — it returns no posts, so it cannot replace it. |
| Worth using at all? | Yes, as an occasional group-discovery tool feeding the post scraper's input list. |
| Best source for the website focus? | Neither. A Facebook Pages scraper plus a platform fingerprint of each page's website. |
