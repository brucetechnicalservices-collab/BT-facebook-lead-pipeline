"""
Airtable logging for Apify scraper runs.

Maintains one record per Apify run in the **Facebook Post Scraper Runs**
table, keyed by ``Apify Run ID``. Every Facebook Raw Signal imported from that
run links back to it, which is what makes this chain answerable:

    Facebook group -> exact Apify run -> exact scrape input -> cost
                   -> resulting posts -> qualified leads -> outreach outcome

Idempotency
-----------

``Apify Run ID`` is the natural key. ``upsert_scraper_run`` looks for an
existing record first and patches it rather than creating a second one, so
re-running the pipeline against the same Apify run (a re-import, a retry, a
manual re-trigger) never produces duplicate run logs.

Honesty about missing values
----------------------------

Cost and Run Input are frequently unavailable — ``usageTotalUsd`` is only
returned to the token that owns the run, and some actors store no INPUT
record. Those columns are omitted from the payload entirely rather than
written as zero or an empty string, so a blank cell means "Apify did not tell
us" and an existing manually-entered value survives.

Like the other new modules this takes an injected Airtable accessor, so the
tests run against recorded payloads with no base and no token.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import redaction

# ---------------------------------------------------------------------------
# Field names
#
# Centralised here rather than scattered through the code, so a rename in
# Airtable is a one-line change. These match the live schema.
# ---------------------------------------------------------------------------

FIELD_RUN_NAME = "Run Name"
FIELD_RUN_INPUT = "Run Input"
FIELD_RESULTS = "Results"
FIELD_COST = "Cost"
FIELD_DURATION = "Duration"
FIELD_RUN_DATE = "Run Date"
FIELD_APIFY_RUN_ID = "Apify Run ID"
FIELD_DATASET_URL = "Dataset URL"

ALL_FIELDS = (
    FIELD_RUN_NAME,
    FIELD_RUN_INPUT,
    FIELD_RESULTS,
    FIELD_COST,
    FIELD_DURATION,
    FIELD_RUN_DATE,
    FIELD_APIFY_RUN_ID,
    FIELD_DATASET_URL,
)

#: Maximum characters written to the Run Input long-text column.
MAX_RUN_INPUT_CHARS = 8000


def escape_formula_value(value: Any) -> str:
    """
    Escape a value for use inside an Airtable filterByFormula string literal.

    Apify run IDs are alphanumeric, so this is belt-and-braces rather than a
    live injection risk — but the lookup is built by string concatenation and
    should not be breakable by a surprising ID.
    """
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def build_lookup_formula(apify_run_id: str) -> str:
    """Build the filterByFormula that finds one run record by its Apify ID."""
    return f"{{{FIELD_APIFY_RUN_ID}}}='{escape_formula_value(apify_run_id)}'"


@dataclass
class ScraperRunRecord:
    """The Airtable record representing one Apify run."""

    record_id: str
    apify_run_id: str
    created: bool

    @property
    def link_value(self) -> list[str]:
        """The value a linked-record field expects: a list of record IDs."""
        return [self.record_id]


def _run_name(run: Any, *, prefix: str = "Facebook scrape") -> str:
    """Build a human-readable run name from the run's start time."""
    started = getattr(run, "started_at", None) or ""
    stamp = str(started).replace("T", " ").replace("Z", "").strip()

    if stamp:
        return f"{prefix} {stamp[:16]} UTC"

    return f"{prefix} {getattr(run, 'id', '')}"


def _serialize_run_input(value: Any) -> str:
    """Render the Apify run input as readable JSON for the long-text column."""
    if value is None:
        return ""

    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, indent=2, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(value)

    return text[:MAX_RUN_INPUT_CHARS]


def build_run_fields(
    run: Any,
    *,
    run_input: Any = None,
    results: int | None = None,
) -> dict[str, Any]:
    """
    Build the Airtable payload for one Apify run.

    Only keys whose values Apify actually supplied are included. A missing
    cost or input is left out of the payload so Airtable keeps whatever is
    already in the cell.
    """
    fields: dict[str, Any] = {
        FIELD_APIFY_RUN_ID: run.id,
        FIELD_RUN_NAME: _run_name(run),
    }

    if run.dataset_url:
        fields[FIELD_DATASET_URL] = run.dataset_url

    if run.started_at:
        fields[FIELD_RUN_DATE] = run.started_at

    if run.duration_seconds is not None:
        fields[FIELD_DURATION] = round(float(run.duration_seconds), 2)

    if run.cost_usd is not None:
        fields[FIELD_COST] = round(float(run.cost_usd), 4)

    count = results if results is not None else run.item_count
    if count is not None:
        fields[FIELD_RESULTS] = int(count)

    serialized_input = _serialize_run_input(run_input)
    if serialized_input:
        fields[FIELD_RUN_INPUT] = serialized_input

    return fields


def find_existing_run(
    lister: Callable[..., list[dict[str, Any]]],
    *,
    apify_run_id: str,
) -> dict[str, Any] | None:
    """Return the existing Airtable record for this Apify run, if any."""
    if not apify_run_id:
        return None

    records = lister(
        formula=build_lookup_formula(apify_run_id),
        fields=[FIELD_APIFY_RUN_ID],
        max_records=1,
    )

    return records[0] if records else None


def upsert_scraper_run(
    run: Any,
    *,
    lister: Callable[..., list[dict[str, Any]]],
    creator: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    updater: Callable[[list[dict[str, Any]]], None],
    run_input: Any = None,
    results: int | None = None,
    dry_run: bool = False,
) -> ScraperRunRecord | None:
    """
    Create or update the Airtable record for one Apify run.

    Returns the record so newly imported Raw Signals can link to it, or None
    in dry-run mode and when Airtable did not return a usable record ID.

    ``Apify Run ID`` is the idempotency key: the same run is never logged
    twice, however many times the pipeline sees it.
    """
    if not getattr(run, "id", ""):
        print(
            "No Apify run ID available; skipping the scraper run log.",
            flush=True,
        )
        return None

    fields = build_run_fields(run, run_input=run_input, results=results)

    if dry_run:
        print(
            f"[DRY RUN] Would upsert scraper run {run.id} "
            f"({fields.get(FIELD_RESULTS, 'unknown')} results, "
            f"cost={fields.get(FIELD_COST, 'unknown')}). Nothing written.",
            flush=True,
        )
        return None

    existing = find_existing_run(lister, apify_run_id=run.id)

    if existing and existing.get("id"):
        record_id = str(existing["id"])
        updater([{"id": record_id, "fields": fields}])

        print(
            f"Updated existing scraper run record "
            f"{redaction.redact_record_id(record_id)} "
            f"for Apify run {run.id}.",
            flush=True,
        )
        return ScraperRunRecord(
            record_id=record_id, apify_run_id=run.id, created=False
        )

    created = creator([{"fields": fields}])
    record_id = str((created[0] or {}).get("id", "")) if created else ""

    if not record_id:
        print(
            f"Airtable did not return a record ID for Apify run {run.id}; "
            f"imported posts will carry the run ID but no linked record.",
            flush=True,
        )
        return None

    print(
        f"Logged Apify run {run.id} as scraper run record "
        f"{redaction.redact_record_id(record_id)}.",
        flush=True,
    )
    return ScraperRunRecord(
        record_id=record_id, apify_run_id=run.id, created=True
    )
