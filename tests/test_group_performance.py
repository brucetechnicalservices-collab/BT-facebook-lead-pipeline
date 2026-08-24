"""
Regression tests for Facebook Group Performance aggregation.

Two things matter most here:

1. The counts are right, including the conservative funnel reading and the
   legacy ``Contacted`` checkbox migration.
2. ``Tier``, ``Status``, ``Revenue``, and ``Notes`` are human-entered and must
   never appear in a payload this module produces.
"""

from __future__ import annotations

import pytest

import group_performance as gp
from tests.fixtures import (
    CRM_RESEARCH,
    GOHIGHLEVEL_PROVIDER,
    PEN_AND_PAPER_ELECTRICIAN,
)

FIELDS = gp.RawSignalFieldNames(
    group_title="Group title",
    text="Text",
    qualified="Qualified",
    outreach_ready="Outreach Ready",
    outreach_status="Outreach Status",
    legacy_contacted="Contacted",
    evidence="Evidence",
    intent_type="Intent Type",
    scraper_run="Scraper Run",
)

GROUP = "Toronto Small Business Owners"
OTHER_GROUP = "GTA Contractors"


def signal(**fields):
    """Build a Raw Signal record with sensible blanks."""
    base = {
        "Group title": GROUP,
        "Text": "",
        "Qualified": False,
        "Outreach Ready": False,
        "Outreach Status": "",
        "Contacted": False,
        "Evidence": "",
        "Intent Type": "",
        "Scraper Run": [],
    }
    return {"id": f"rec{len(fields)}{hash(str(fields)) % 10000}",
            "fields": {**base, **fields}}


def aggregate(records, run_dates=None):
    return gp.aggregate_raw_signals(
        records, fields=FIELDS, run_dates=run_dates
    )


def metrics_for(records, group=GROUP, run_dates=None):
    result = aggregate(records, run_dates=run_dates)
    return result.metrics[gp.normalize_group_name(group)]


# ---------------------------------------------------------------------------
# Basic counts
# ---------------------------------------------------------------------------

def test_posts_scraped_counts_every_record_for_the_group():
    metrics = metrics_for([signal(), signal(), signal()])

    assert metrics.posts_scraped == 3


def test_ai_candidates_counts_records_with_model_evidence():
    """
    Evidence is only ever written from model output, so it distinguishes
    posts that cost a token from posts the prefilter rejected for free.
    """
    metrics = metrics_for(
        [
            signal(Evidence="Asked for a developer."),
            signal(Evidence=""),          # prefiltered, never saw the model
            signal(Evidence="Wants a CRM."),
        ]
    )

    assert metrics.ai_candidates == 2
    assert metrics.posts_scraped == 3


def test_system_qualified_counts_the_qualified_flag():
    metrics = metrics_for(
        [signal(Qualified=True), signal(Qualified=True), signal()]
    )

    assert metrics.system_qualified == 2


def test_outreach_ready_is_counted_separately_from_qualified():
    metrics = metrics_for(
        [
            signal(Qualified=True, **{"Outreach Ready": True}),
            signal(Qualified=True),
        ]
    )

    assert metrics.system_qualified == 2
    assert metrics.outreach_ready == 1


# ---------------------------------------------------------------------------
# Requirement 27: provider requests are counted correctly
# ---------------------------------------------------------------------------

def test_provider_requests_are_counted_from_the_post_text():
    """
    Recomputed in Python rather than read from the Airtable formula, so the
    count does not depend on formula calculation having caught up.
    """
    metrics = metrics_for(
        [
            signal(Text=GOHIGHLEVEL_PROVIDER),          # provider request
            signal(Text=PEN_AND_PAPER_ELECTRICIAN),     # business pain
            signal(Text=CRM_RESEARCH),                  # tool research
        ]
    )

    assert metrics.provider_requests == 1
    assert metrics.posts_scraped == 3


def test_provider_requests_fall_back_to_the_formula_when_text_is_missing():
    metrics = metrics_for(
        [signal(Text="", **{"Intent Type": "Provider Request"})]
    )

    assert metrics.provider_requests == 1


# ---------------------------------------------------------------------------
# Funnel semantics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,contacted,replies,meetings,proposals,won",
    [
        ("Not Contacted", 0, 0, 0, 0, 0),
        ("Do Not Contact", 0, 0, 0, 0, 0),
        ("Contacted", 1, 0, 0, 0, 0),
        ("No Response", 1, 0, 0, 0, 0),
        ("Replied", 1, 1, 0, 0, 0),
        ("Meeting Booked", 1, 1, 1, 0, 0),
        ("Proposal Sent", 1, 1, 1, 1, 0),
        ("Won", 1, 1, 1, 1, 1),
        # Lost is deliberately conservative: a deal can be lost at any stage,
        # so it never implies a meeting or a proposal.
        ("Lost", 1, 1, 0, 0, 0),
    ],
)
def test_funnel_progression(status, contacted, replies, meetings, proposals, won):
    metrics = metrics_for([signal(**{"Outreach Status": status})])

    assert metrics.contacted == contacted
    assert metrics.replies == replies
    assert metrics.meetings == meetings
    assert metrics.proposals == proposals
    assert metrics.won == won


def test_status_matching_is_case_and_space_insensitive():
    metrics = metrics_for([signal(**{"Outreach Status": "  MEETING  BOOKED "})])

    assert metrics.meetings == 1


# ---------------------------------------------------------------------------
# Legacy Contacted checkbox migration
# ---------------------------------------------------------------------------

def test_legacy_contacted_counts_when_outreach_status_is_empty():
    metrics = metrics_for([signal(Contacted=True)])

    assert metrics.contacted == 1


def test_legacy_contacted_does_not_double_count_a_migrated_record():
    """Both fields set: the new field wins, the record counts once."""
    metrics = metrics_for(
        [signal(Contacted=True, **{"Outreach Status": "Replied"})]
    )

    assert metrics.contacted == 1
    assert metrics.replies == 1


def test_legacy_contacted_false_counts_nothing():
    assert metrics_for([signal(Contacted=False)]).contacted == 0


# ---------------------------------------------------------------------------
# Requirement 28: human-owned columns are never written
# ---------------------------------------------------------------------------

def test_payload_never_contains_human_owned_fields():
    fields = metrics_for([signal(Qualified=True)]).to_fields()

    assert gp.FIELD_TIER not in fields
    assert gp.FIELD_STATUS not in fields
    assert gp.FIELD_REVENUE not in fields
    assert gp.FIELD_NOTES not in fields


def test_payload_only_contains_computed_fields():
    fields = metrics_for([signal()]).to_fields()

    assert set(fields) <= set(gp.COMPUTED_FIELDS)


def test_updates_preserve_existing_human_data():
    aggregation = aggregate([signal(Qualified=True)])
    performance = [
        {
            "id": "recGroup1",
            "fields": {
                gp.FIELD_GROUP_NAME: GROUP,
                gp.FIELD_TIER: "A",
                gp.FIELD_STATUS: "Active",
                gp.FIELD_REVENUE: 12000,
                gp.FIELD_NOTES: "Best group so far.",
            },
        }
    ]

    updates, unmatched = gp.build_group_updates(aggregation, performance)

    assert unmatched == []
    assert len(updates) == 1
    assert not set(updates[0]["fields"]) & gp.NEVER_WRITE_FIELDS


# ---------------------------------------------------------------------------
# Last Run and matching
# ---------------------------------------------------------------------------

def test_last_run_uses_the_latest_linked_scraper_run():
    metrics = metrics_for(
        [
            signal(**{"Scraper Run": ["recRunA"]}),
            signal(**{"Scraper Run": ["recRunB"]}),
        ],
        run_dates={
            "recRunA": "2026-08-10T06:30:00.000Z",
            "recRunB": "2026-08-18T06:30:00.000Z",
        },
    )

    assert metrics.last_run == "2026-08-18T06:30:00.000Z"


def test_records_without_a_scraper_run_link_do_not_fabricate_last_run():
    """Historical records predate run attribution. That is fine."""
    metrics = metrics_for([signal(), signal()])

    assert metrics.last_run is None
    assert gp.FIELD_LAST_RUN not in metrics.to_fields()


def test_group_matching_ignores_case_and_spacing():
    aggregation = aggregate([signal(**{"Group title": "  toronto   TRADES "})])
    performance = [
        {"id": "recG", "fields": {gp.FIELD_GROUP_NAME: "Toronto Trades"}}
    ]

    updates, unmatched = gp.build_group_updates(aggregation, performance)

    assert unmatched == []
    assert updates[0]["id"] == "recG"


def test_groups_without_a_performance_record_are_reported_not_created():
    aggregation = aggregate([signal()])

    updates, unmatched = gp.build_group_updates(aggregation, [])

    assert updates == []
    assert unmatched == [GROUP]


def test_records_without_a_group_are_skipped_gracefully():
    result = aggregate([signal(**{"Group title": ""}), signal()])

    assert result.records_seen == 2
    assert result.records_without_group == 1
    assert len(result.metrics) == 1


def test_multiple_groups_are_kept_separate():
    result = aggregate(
        [
            signal(Qualified=True),
            signal(**{"Group title": OTHER_GROUP}),
        ]
    )

    assert len(result.metrics) == 2
    assert result.metrics[gp.normalize_group_name(GROUP)].system_qualified == 1
    assert (
        result.metrics[gp.normalize_group_name(OTHER_GROUP)].system_qualified
        == 0
    )


# ---------------------------------------------------------------------------
# Full refresh
# ---------------------------------------------------------------------------

def test_refresh_writes_only_matched_groups():
    written: list[list[dict]] = []

    gp.refresh_group_performance(
        raw_signal_lister=lambda **_: [signal(Qualified=True)],
        performance_lister=lambda **_: [
            {"id": "recG", "fields": {gp.FIELD_GROUP_NAME: GROUP}}
        ],
        performance_updater=written.append,
        fields=FIELDS,
    )

    assert len(written) == 1
    assert written[0][0]["id"] == "recG"
    assert written[0][0]["fields"][gp.FIELD_SYSTEM_QUALIFIED] == 1


def test_refresh_in_dry_run_writes_nothing():
    written: list[list[dict]] = []

    gp.refresh_group_performance(
        raw_signal_lister=lambda **_: [signal(Qualified=True)],
        performance_lister=lambda **_: [
            {"id": "recG", "fields": {gp.FIELD_GROUP_NAME: GROUP}}
        ],
        performance_updater=written.append,
        fields=FIELDS,
        dry_run=True,
    )

    assert written == []
