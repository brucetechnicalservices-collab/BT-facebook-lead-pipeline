"""
Regression tests for exact Apify run attribution and Airtable linkage.

The bug these guard against: the pipeline used to read
``/actor-tasks/{id}/runs/last/dataset/items``, which returns items with no run
identity attached. A manual Apify run finishing between the scheduled scrape
and this read would silently substitute its own posts, and either way no run
ID was recorded — so cost per lead and "which scrape found this customer?"
were unanswerable.

Every Apify and Airtable call here is mocked. No test starts a paid actor run,
touches a real base, or spends an OpenAI token.
"""

from __future__ import annotations

import json

import pytest

import apify_runs
import scraper_runs

# ---------------------------------------------------------------------------
# Recorded Apify payloads
#
# Shapes taken from the documented Apify API v2 run object. Only fields the
# code actually reads are populated.
# ---------------------------------------------------------------------------

RUN_ID = "abc123RUNID"
DATASET_ID = "ds000000001"
OTHER_RUN_ID = "zzz999OTHER"
OTHER_DATASET_ID = "ds999999999"

SUCCEEDED_RUN = {
    "id": RUN_ID,
    "actId": "actor123",
    "actorTaskId": "task123",
    "status": "SUCCEEDED",
    "startedAt": "2026-08-18T06:30:00.000Z",
    "finishedAt": "2026-08-18T06:41:30.000Z",
    "defaultDatasetId": DATASET_ID,
    "defaultKeyValueStoreId": "kvs00000001",
    "stats": {"runTimeSecs": 690.5},
    "usageTotalUsd": 0.4213,
}

RUNNING_RUN = {**SUCCEEDED_RUN, "status": "RUNNING", "finishedAt": None}
FAILED_RUN = {**SUCCEEDED_RUN, "status": "FAILED"}
ABORTED_RUN = {**SUCCEEDED_RUN, "status": "ABORTED"}
TIMED_OUT_RUN = {**SUCCEEDED_RUN, "status": "TIMED-OUT"}

DATASET_ITEMS = [
    {
        "legacyId": "111",
        "url": "https://www.facebook.com/groups/1/posts/111",
        "text": "Looking for a web developer to rebuild our site.",
        "user": {"id": "u1", "name": "Alex"},
        "groupTitle": "Toronto Trades",
        "time": "2026-08-17T09:00:00Z",
        "commentsCount": 3,
    },
    {
        "legacyId": "222",
        "url": "https://www.facebook.com/groups/1/posts/222",
        "text": "Need our CRM connected to our booking system.",
        "user": {"id": "u2", "name": "Bev"},
        "groupTitle": "Toronto Trades",
        "time": "2026-08-17T10:00:00Z",
        "commentsCount": 1,
    },
    # Apify marks unusable rows with an error field; these must be dropped.
    {"url": "https://www.facebook.com/x", "error": "no-content"},
]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeApify:
    """Records every request so tests can assert on exactly what was called."""

    def __init__(self, *, run_sequence=None, dataset_items=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.run_sequence = list(run_sequence or [SUCCEEDED_RUN])
        self.dataset_items = (
            dataset_items if dataset_items is not None else DATASET_ITEMS
        )
        self.started = 0

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))

        if method == "POST" and url.endswith("/runs"):
            self.started += 1
            return FakeResponse({"data": RUNNING_RUN})

        if "/runs/last" in url:
            return FakeResponse({"data": self.run_sequence[-1]})

        if "/actor-runs/" in url:
            payload = self.run_sequence[0]
            if len(self.run_sequence) > 1:
                self.run_sequence.pop(0)
            return FakeResponse({"data": payload})

        if url.endswith(f"/datasets/{DATASET_ID}/items"):
            return FakeResponse(self.dataset_items)

        if url.endswith(f"/datasets/{OTHER_DATASET_ID}/items"):
            raise AssertionError(
                "fetched the wrong dataset -- run attribution is broken"
            )

        if "records/INPUT" in url:
            return FakeResponse({"startUrls": [{"url": "https://fb/g/1"}]})

        raise AssertionError(f"unexpected request: {method} {url}")

    def urls(self) -> list[str]:
        return [url for _, url, _ in self.calls]


# ---------------------------------------------------------------------------
# Run parsing
# ---------------------------------------------------------------------------

def test_parse_run_reads_documented_fields():
    run = apify_runs.parse_run({"data": SUCCEEDED_RUN})

    assert run.id == RUN_ID
    assert run.dataset_id == DATASET_ID
    assert run.status == "SUCCEEDED"
    assert run.duration_seconds == pytest.approx(690.5)
    assert run.cost_usd == pytest.approx(0.4213)
    assert run.dataset_url.endswith(DATASET_ID)
    assert run.console_url.endswith(RUN_ID)


def test_duration_falls_back_to_timestamps():
    payload = {**SUCCEEDED_RUN}
    payload.pop("stats")

    run = apify_runs.parse_run(payload)

    assert run.duration_seconds == pytest.approx(690.0)


def test_missing_cost_is_none_not_zero():
    """A cost we were not told is unknown, not free."""
    payload = {**SUCCEEDED_RUN}
    payload.pop("usageTotalUsd")

    assert apify_runs.parse_run(payload).cost_usd is None


def test_run_without_an_id_is_rejected():
    with pytest.raises(apify_runs.ApifyError):
        apify_runs.parse_run({"data": {"status": "SUCCEEDED"}})


# ---------------------------------------------------------------------------
# Requirement 23: the exact run's dataset is used
# ---------------------------------------------------------------------------

def test_exact_run_dataset_is_used_not_an_unrelated_last_dataset():
    """
    The whole point of this release.

    The run object is resolved first, and items come from *that run's*
    defaultDatasetId. The old ``/runs/last/dataset/items`` shortcut must not
    appear anywhere in the request log.
    """
    apify = FakeApify()

    run = apify_runs.resolve_run(apify, token="t", task_id="task123")
    posts = apify_runs.collect_posts(apify, token="t", run=run)

    assert run.id == RUN_ID
    assert run.dataset_id == DATASET_ID
    assert len(posts) == 2

    urls = apify.urls()
    assert any(f"/datasets/{DATASET_ID}/items" in url for url in urls)
    assert not any("runs/last/dataset" in url for url in urls)


def test_a_concurrent_run_cannot_substitute_its_dataset():
    """
    Resolve one run, then have the API start reporting a different one.

    Items must still come from the originally resolved dataset. FakeApify
    raises if the other dataset is ever requested.
    """
    apify = FakeApify()
    run = apify_runs.resolve_run(apify, token="t", task_id="task123")

    apify.run_sequence = [
        {**SUCCEEDED_RUN, "id": OTHER_RUN_ID,
         "defaultDatasetId": OTHER_DATASET_ID}
    ]

    posts = apify_runs.collect_posts(apify, token="t", run=run)

    assert len(posts) == 2
    assert run.id == RUN_ID


def test_started_run_is_polled_by_its_own_id():
    apify = FakeApify(run_sequence=[RUNNING_RUN, SUCCEEDED_RUN])

    run = apify_runs.resolve_run(
        apify,
        token="t",
        task_id="task123",
        start_new_run=True,
        poll_seconds=1,
        sleep=lambda _: None,
    )

    assert apify.started == 1
    assert run.id == RUN_ID
    assert run.status == "SUCCEEDED"
    assert any(f"/actor-runs/{RUN_ID}" in url for url in apify.urls())


def test_pinned_run_id_overrides_everything():
    apify = FakeApify()

    run = apify_runs.resolve_run(
        apify, token="t", task_id="task123", start_new_run=True,
        run_id=RUN_ID,
    )

    assert apify.started == 0
    assert run.id == RUN_ID


@pytest.mark.parametrize(
    "payload", [FAILED_RUN, ABORTED_RUN, TIMED_OUT_RUN]
)
def test_failed_run_states_raise(payload):
    apify = FakeApify(run_sequence=[payload])

    with pytest.raises(apify_runs.ApifyRunFailed):
        apify_runs.wait_for_run(
            apify,
            token="t",
            run_id=RUN_ID,
            timeout_seconds=10,
            poll_seconds=1,
            sleep=lambda _: None,
        )


def test_polling_timeout_raises_distinctly():
    apify = FakeApify(run_sequence=[RUNNING_RUN])
    clock = iter([0.0, 100.0, 200.0])

    with pytest.raises(apify_runs.ApifyRunNotFinished):
        apify_runs.wait_for_run(
            apify,
            token="t",
            run_id=RUN_ID,
            timeout_seconds=1,
            poll_seconds=1,
            sleep=lambda _: None,
            monotonic=lambda: next(clock),
        )


def test_run_without_a_dataset_is_an_error():
    payload = {**SUCCEEDED_RUN, "defaultDatasetId": ""}
    apify = FakeApify(run_sequence=[payload])

    with pytest.raises(apify_runs.ApifyError, match="dataset"):
        apify_runs.resolve_run(apify, token="t", task_id="task123")


def test_missing_last_run_gives_an_actionable_error():
    def empty(method, url, **kwargs):
        return FakeResponse({"data": None})

    with pytest.raises(apify_runs.ApifyError, match="RUN_APIFY_TASK"):
        apify_runs.resolve_run(empty, token="t", task_id="task123")


def test_token_is_sent_as_a_header_not_a_query_parameter():
    """A token in a URL ends up in logs and exception messages."""
    apify = FakeApify()
    apify_runs.resolve_run(apify, token="secret-token", task_id="task123")

    for _, url, kwargs in apify.calls:
        assert "secret-token" not in url
        assert kwargs.get("headers", {}).get("Authorization")
        assert "token" not in (kwargs.get("params") or {})


def test_item_count_records_the_raw_dataset_size():
    apify = FakeApify()
    run = apify_runs.resolve_run(apify, token="t", task_id="task123")
    apify_runs.collect_posts(apify, token="t", run=run)

    assert run.item_count == 3  # including the errored row


# ---------------------------------------------------------------------------
# Requirements 24 and 26: scraper run upsert, keyed by Apify Run ID
# ---------------------------------------------------------------------------

class FakeAirtable:
    """Minimal in-memory Airtable double for one table."""

    def __init__(self, existing=None):
        self.records = list(existing or [])
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self._next = 1

    def list(self, *, formula=None, fields=None, max_records=None, **kwargs):
        if formula is None:
            return list(self.records)

        matched = [
            record
            for record in self.records
            if f"'{record['fields'].get(scraper_runs.FIELD_APIFY_RUN_ID)}'"
            in formula
        ]
        return matched[:max_records] if max_records else matched

    def create(self, records):
        made = []
        for record in records:
            self._next += 1
            new = {"id": f"recNEW{self._next}", "fields": record["fields"]}
            self.records.append(new)
            self.created.append(new)
            made.append(new)
        return made

    def update(self, updates):
        self.updated.extend(updates)


def upsert(run, airtable, **kwargs):
    return scraper_runs.upsert_scraper_run(
        run,
        lister=airtable.list,
        creator=airtable.create,
        updater=airtable.update,
        **kwargs,
    )


def test_scraper_run_record_is_created_with_real_metadata():
    run = apify_runs.parse_run(SUCCEEDED_RUN)
    run.item_count = 2
    airtable = FakeAirtable()

    record = upsert(run, airtable, run_input={"startUrls": ["x"]})

    assert record is not None
    assert record.created is True

    fields = airtable.created[0]["fields"]
    assert fields[scraper_runs.FIELD_APIFY_RUN_ID] == RUN_ID
    assert fields[scraper_runs.FIELD_RESULTS] == 2
    assert fields[scraper_runs.FIELD_COST] == pytest.approx(0.4213)
    assert fields[scraper_runs.FIELD_DURATION] == pytest.approx(690.5)
    assert DATASET_ID in fields[scraper_runs.FIELD_DATASET_URL]
    assert "startUrls" in fields[scraper_runs.FIELD_RUN_INPUT]


def test_same_apify_run_does_not_create_a_duplicate_record():
    """Requirement 26. Apify Run ID is the idempotency key."""
    run = apify_runs.parse_run(SUCCEEDED_RUN)
    airtable = FakeAirtable()

    first = upsert(run, airtable)
    second = upsert(run, airtable)

    assert len(airtable.created) == 1
    assert first.record_id == second.record_id
    assert second.created is False
    assert len(airtable.updated) == 1


def test_missing_cost_is_omitted_rather_than_written_as_zero():
    payload = {**SUCCEEDED_RUN}
    payload.pop("usageTotalUsd")
    run = apify_runs.parse_run(payload)

    fields = scraper_runs.build_run_fields(run)

    assert scraper_runs.FIELD_COST not in fields


def test_missing_run_input_is_omitted():
    run = apify_runs.parse_run(SUCCEEDED_RUN)

    fields = scraper_runs.build_run_fields(run, run_input=None)

    assert scraper_runs.FIELD_RUN_INPUT not in fields


def test_dry_run_writes_nothing():
    run = apify_runs.parse_run(SUCCEEDED_RUN)
    airtable = FakeAirtable()

    assert upsert(run, airtable, dry_run=True) is None
    assert airtable.created == []
    assert airtable.updated == []


def test_formula_value_is_escaped():
    formula = scraper_runs.build_lookup_formula("it's-a-run")

    assert "\\'" in formula


def test_run_input_is_truncated():
    run = apify_runs.parse_run(SUCCEEDED_RUN)

    fields = scraper_runs.build_run_fields(
        run, run_input={"blob": "x" * 50_000}
    )

    assert len(fields[scraper_runs.FIELD_RUN_INPUT]) <= (
        scraper_runs.MAX_RUN_INPUT_CHARS
    )


def test_run_input_is_valid_readable_json():
    run = apify_runs.parse_run(SUCCEEDED_RUN)

    fields = scraper_runs.build_run_fields(
        run, run_input={"startUrls": [{"url": "https://fb/g/1"}]}
    )

    assert json.loads(fields[scraper_runs.FIELD_RUN_INPUT]) == {
        "startUrls": [{"url": "https://fb/g/1"}]
    }
