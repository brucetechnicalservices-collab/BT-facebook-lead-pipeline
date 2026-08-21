"""
Facebook Group Performance aggregation.

Closes the measurement loop:

    group -> posts scraped -> AI candidates -> qualified -> outreach ready
          -> contacted -> replied -> meeting -> proposal -> won

Counts are derived from the **Facebook Raw Signals** table, which is the
source of truth for everything operational. This module writes only the
computed columns.

What this module never touches
------------------------------

``Tier``, ``Status``, ``Revenue``, and ``Notes`` are strategy and sales data
entered by a human. They are absent from every payload built here, so a
pipeline run cannot flatten them. ``NEVER_WRITE_FIELDS`` is asserted against
each payload before it is returned, so adding a field to the writer by mistake
fails loudly in the tests rather than quietly in production.

Funnel semantics
----------------

``Outreach Status`` is a single select holding the *current* state of a lead,
so later states imply the earlier ones. The mapping is deliberately
conservative where a status does not guarantee a stage happened:

===================  =========  =======  ========  =========  ===
Outreach Status      Contacted  Replies  Meetings  Proposals  Won
===================  =========  =======  ========  =========  ===
Contacted            yes        --       --        --         --
No Response          yes        --       --        --         --
Replied              yes        yes      --        --         --
Meeting Booked       yes        yes      yes       --         --
Proposal Sent        yes        yes      yes       yes        --
Won                  yes        yes      yes       yes        yes
Lost                 yes        yes      --        --         --
Do Not Contact       --         --       --        --         --
Not Contacted        --         --       --        --         --
===================  =========  =======  ========  =========  ===

``Lost`` counts as contacted and replied but not as a meeting or proposal: a
deal can be lost at any stage, and inferring a meeting from it would inflate
the funnel. ``Won`` implies the full progression.

Legacy ``Contacted`` checkbox
-----------------------------

The table still carries the original ``Contacted`` checkbox. It is preserved,
never written, and counted only when ``Outreach Status`` is empty — so records
migrated to the new field are not double-counted, and records that predate it
still register.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import intent
import redaction

# ---------------------------------------------------------------------------
# Field names
# ---------------------------------------------------------------------------

FIELD_GROUP_NAME = "Group Name"
FIELD_GROUP_URL = "Group URL"
FIELD_TIER = "Tier"
FIELD_STATUS = "Status"
FIELD_POSTS_SCRAPED = "Posts Scraped"
FIELD_AI_CANDIDATES = "AI Candidates"
FIELD_SYSTEM_QUALIFIED = "System Qualified"
FIELD_PROVIDER_REQUESTS = "Provider Requests"
FIELD_OUTREACH_READY = "Outreach Ready"
FIELD_CONTACTED = "Contacted"
FIELD_REPLIES = "Replies"
FIELD_MEETINGS = "Meetings"
FIELD_PROPOSALS = "Proposals"
FIELD_WON = "Won"
FIELD_REVENUE = "Revenue"
FIELD_LAST_RUN = "Last Run"
FIELD_NOTES = "Notes"

#: Human-owned columns. Never written by this module, at any time.
NEVER_WRITE_FIELDS = frozenset(
    {FIELD_TIER, FIELD_STATUS, FIELD_REVENUE, FIELD_NOTES}
)

#: The columns this module maintains.
COMPUTED_FIELDS = (
    FIELD_POSTS_SCRAPED,
    FIELD_AI_CANDIDATES,
    FIELD_SYSTEM_QUALIFIED,
    FIELD_PROVIDER_REQUESTS,
    FIELD_OUTREACH_READY,
    FIELD_CONTACTED,
    FIELD_REPLIES,
    FIELD_MEETINGS,
    FIELD_PROPOSALS,
    FIELD_WON,
    FIELD_LAST_RUN,
)

# ---------------------------------------------------------------------------
# Outreach Status vocabulary
# ---------------------------------------------------------------------------

STATUS_NOT_CONTACTED = "not contacted"
STATUS_CONTACTED = "contacted"
STATUS_REPLIED = "replied"
STATUS_MEETING_BOOKED = "meeting booked"
STATUS_PROPOSAL_SENT = "proposal sent"
STATUS_WON = "won"
STATUS_LOST = "lost"
STATUS_NO_RESPONSE = "no response"
STATUS_DO_NOT_CONTACT = "do not contact"

CONTACTED_STATUSES = frozenset(
    {
        STATUS_CONTACTED,
        STATUS_NO_RESPONSE,
        STATUS_REPLIED,
        STATUS_MEETING_BOOKED,
        STATUS_PROPOSAL_SENT,
        STATUS_WON,
        STATUS_LOST,
    }
)

REPLIED_STATUSES = frozenset(
    {
        STATUS_REPLIED,
        STATUS_MEETING_BOOKED,
        STATUS_PROPOSAL_SENT,
        STATUS_WON,
        STATUS_LOST,
    }
)

MEETING_STATUSES = frozenset(
    {STATUS_MEETING_BOOKED, STATUS_PROPOSAL_SENT, STATUS_WON}
)

PROPOSAL_STATUSES = frozenset({STATUS_PROPOSAL_SENT, STATUS_WON})

WON_STATUSES = frozenset({STATUS_WON})

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_status(value: Any) -> str:
    """Normalise an Outreach Status cell for comparison."""
    return _WHITESPACE_RE.sub(" ", str(value or "").strip().lower())


def normalize_group_name(value: Any) -> str:
    """
    Normalise a group name so Raw Signals and the performance table match.

    Group titles arrive from Facebook with inconsistent spacing and casing.
    """
    return _WHITESPACE_RE.sub(" ", str(value or "").strip().lower())


def _as_bool(value: Any) -> bool:
    """Coerce an Airtable checkbox value to a bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "yes", "y", "1", "checked"}


@dataclass
class GroupMetrics:
    """Computed counts for one Facebook group."""

    group_name: str
    posts_scraped: int = 0
    ai_candidates: int = 0
    system_qualified: int = 0
    provider_requests: int = 0
    outreach_ready: int = 0
    contacted: int = 0
    replies: int = 0
    meetings: int = 0
    proposals: int = 0
    won: int = 0
    last_run: str | None = None

    def to_fields(self) -> dict[str, Any]:
        """
        Build the Airtable payload.

        Only computed columns appear. ``Last Run`` is omitted when no linked
        scraper run supplied a date, so a historical value is never blanked.
        """
        fields: dict[str, Any] = {
            FIELD_POSTS_SCRAPED: self.posts_scraped,
            FIELD_AI_CANDIDATES: self.ai_candidates,
            FIELD_SYSTEM_QUALIFIED: self.system_qualified,
            FIELD_PROVIDER_REQUESTS: self.provider_requests,
            FIELD_OUTREACH_READY: self.outreach_ready,
            FIELD_CONTACTED: self.contacted,
            FIELD_REPLIES: self.replies,
            FIELD_MEETINGS: self.meetings,
            FIELD_PROPOSALS: self.proposals,
            FIELD_WON: self.won,
        }

        if self.last_run:
            fields[FIELD_LAST_RUN] = self.last_run

        # Structural guarantee, not a comment: human-owned columns can never
        # leave this method.
        assert not (set(fields) & NEVER_WRITE_FIELDS), (
            "group performance payload must never contain "
            f"{sorted(set(fields) & NEVER_WRITE_FIELDS)}"
        )

        return fields


@dataclass
class RawSignalFieldNames:
    """
    The Raw Signals column names this module reads.

    Injected rather than imported so run_pipeline stays the single place that
    knows the Raw Signals schema.
    """

    group_title: str
    text: str
    qualified: str
    outreach_ready: str
    outreach_status: str
    legacy_contacted: str
    evidence: str
    intent_type: str
    scraper_run: str


@dataclass
class AggregationResult:
    """Metrics keyed by normalised group name, plus anything unmatched."""

    metrics: dict[str, GroupMetrics] = field(default_factory=dict)
    records_seen: int = 0
    records_without_group: int = 0


def aggregate_raw_signals(
    records: Iterable[dict[str, Any]],
    *,
    fields: RawSignalFieldNames,
    run_dates: dict[str, str] | None = None,
) -> AggregationResult:
    """
    Roll Raw Signal records up into per-group metrics.

    ``run_dates`` maps a scraper-run record ID to its Run Date. When a record
    has no scraper-run link — every record imported before run attribution
    existed — it simply contributes nothing to ``Last Run``. Historical links
    are never fabricated.

    **AI Candidates** counts records with non-blank ``Evidence``. Evidence is
    only ever written from model output, so it distinguishes posts that
    actually cost a token from posts the prefilter rejected without one.
    Records processed by the legacy pipeline may lack it; see
    docs/PIPELINE_AUDIT.md.

    **Provider Requests** is recomputed in Python from the post text rather
    than read from the Airtable ``Intent Type`` formula, so the count does not
    depend on formula calculation having caught up. The formula value is used
    only when the text is missing.
    """
    result = AggregationResult()
    dates = run_dates or {}

    for record in records:
        result.records_seen += 1
        record_fields = record.get("fields", {}) or {}

        raw_group = record_fields.get(fields.group_title)
        key = normalize_group_name(raw_group)

        if not key:
            result.records_without_group += 1
            continue

        metrics = result.metrics.get(key)
        if metrics is None:
            metrics = GroupMetrics(group_name=str(raw_group).strip())
            result.metrics[key] = metrics

        metrics.posts_scraped += 1

        if str(record_fields.get(fields.evidence, "") or "").strip():
            metrics.ai_candidates += 1

        if _as_bool(record_fields.get(fields.qualified)):
            metrics.system_qualified += 1

        if _as_bool(record_fields.get(fields.outreach_ready)):
            metrics.outreach_ready += 1

        if _is_provider_request(record_fields, fields):
            metrics.provider_requests += 1

        status = normalize_status(record_fields.get(fields.outreach_status))

        if status:
            if status in CONTACTED_STATUSES:
                metrics.contacted += 1
            if status in REPLIED_STATUSES:
                metrics.replies += 1
            if status in MEETING_STATUSES:
                metrics.meetings += 1
            if status in PROPOSAL_STATUSES:
                metrics.proposals += 1
            if status in WON_STATUSES:
                metrics.won += 1
        elif _as_bool(record_fields.get(fields.legacy_contacted)):
            # Legacy checkbox only counts when the new field is empty, so a
            # migrated record is never counted twice.
            metrics.contacted += 1

        run_date = _latest_run_date(
            record_fields.get(fields.scraper_run), dates
        )
        if run_date and (
            metrics.last_run is None or run_date > metrics.last_run
        ):
            metrics.last_run = run_date

    return result


def _is_provider_request(
    record_fields: dict[str, Any],
    fields: RawSignalFieldNames,
) -> bool:
    """Was this post a request for a provider?"""
    text = record_fields.get(fields.text)

    if str(text or "").strip():
        classified = intent.classify_intent(text)
        return classified.intent_type == intent.INTENT_PROVIDER_REQUEST

    # No text to classify: fall back to the Airtable formula value.
    label = str(record_fields.get(fields.intent_type, "") or "").strip().lower()
    return label == intent.INTENT_LABELS[
        intent.INTENT_PROVIDER_REQUEST
    ].lower()


def _latest_run_date(
    links: Any,
    run_dates: dict[str, str],
) -> str | None:
    """Return the latest known Run Date across a record's scraper-run links."""
    if not links:
        return None

    if isinstance(links, str):
        candidates = [links]
    elif isinstance(links, (list, tuple)):
        candidates = [str(item) for item in links if item]
    else:
        return None

    dates = [run_dates[item] for item in candidates if item in run_dates]
    return max(dates) if dates else None


def build_group_updates(
    aggregation: AggregationResult,
    performance_records: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Match aggregated metrics onto existing performance records.

    Returns the Airtable PATCH payloads and the names of groups that have
    metrics but no performance record. Unmatched groups are reported, never
    auto-created: the performance table carries human strategy data and its
    rows are curated deliberately.
    """
    by_name: dict[str, str] = {}

    for record in performance_records:
        record_fields = record.get("fields", {}) or {}
        key = normalize_group_name(record_fields.get(FIELD_GROUP_NAME))

        if key and record.get("id"):
            by_name[key] = str(record["id"])

    updates: list[dict[str, Any]] = []
    unmatched: list[str] = []

    for key, metrics in sorted(aggregation.metrics.items()):
        record_id = by_name.get(key)

        if not record_id:
            unmatched.append(metrics.group_name)
            continue

        updates.append({"id": record_id, "fields": metrics.to_fields()})

    return updates, unmatched


def refresh_group_performance(
    *,
    raw_signal_lister: Callable[..., list[dict[str, Any]]],
    performance_lister: Callable[..., list[dict[str, Any]]],
    performance_updater: Callable[[list[dict[str, Any]]], None],
    fields: RawSignalFieldNames,
    run_dates: dict[str, str] | None = None,
    dry_run: bool = False,
) -> AggregationResult:
    """
    Recompute every group's operational counts and write them to Airtable.

    Reads the whole Raw Signals table. That is one paged read per pipeline
    run, which is cheap next to the AI calls and keeps the counts exact rather
    than incremental (and therefore drift-prone).
    """
    records = raw_signal_lister(
        fields=[
            fields.group_title,
            fields.text,
            fields.qualified,
            fields.outreach_ready,
            fields.outreach_status,
            fields.legacy_contacted,
            fields.evidence,
            fields.intent_type,
            fields.scraper_run,
        ]
    )

    aggregation = aggregate_raw_signals(
        records, fields=fields, run_dates=run_dates
    )

    performance_records = performance_lister(fields=[FIELD_GROUP_NAME])
    updates, unmatched = build_group_updates(aggregation, performance_records)

    if unmatched:
        print(
            "Groups with activity but no Facebook Group Performance record "
            f"(add them manually to track): {', '.join(sorted(unmatched))}",
            flush=True,
        )

    if not updates:
        print("No group performance records to update.", flush=True)
        return aggregation

    if dry_run:
        for position, update in enumerate(updates, start=1):
            print(
                f"[DRY RUN] Would update group "
                f"[{position}/{len(updates)}] "
                f"{redaction.redact_record_id(update['id'])}: "
                f"{update['fields']}",
                flush=True,
            )
        return aggregation

    performance_updater(updates)
    print(
        f"Refreshed {len(updates)} Facebook Group Performance records.",
        flush=True,
    )
    return aggregation
