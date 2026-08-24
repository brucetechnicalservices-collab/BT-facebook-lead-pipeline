"""
Exact Apify run resolution and dataset retrieval.

The problem this module fixes
-----------------------------

The pipeline used to read posts from::

    GET /v2/actor-tasks/{taskId}/runs/last/dataset/items

That endpoint hands back *items* without ever telling you which run produced
them. When a scheduled GitHub run and a manual Apify run overlap — which they
routinely did — the pipeline could import a different run's posts than the one
it believed it was reading, and there was no run ID to record either way. Cost
per lead, group performance, and "which scrape found this customer?" were all
unanswerable.

Every path here resolves **one exact run object first**, reads its
``defaultDatasetId``, and only then fetches items. The run ID travels with the
posts into Airtable, so every Raw Signal can be traced back to the scrape that
produced it.

Design
------

All network access goes through an injected ``requester`` callable with the
signature of ``run_pipeline.request_with_retry``::

    requester(method, url, headers=..., params=..., json=...) -> response

That keeps this module free of ``requests``, credentials, and retry policy, so
the tests exercise real parsing logic against recorded payloads without
touching the network or spending a cent on a paid actor run.

Only documented Apify API v2 fields are read. Anything absent stays ``None``
rather than being guessed at — a blank Cost column is honest, an invented one
is not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

API_BASE = "https://api.apify.com/v2"
CONSOLE_BASE = "https://console.apify.com"

#: Apify run statuses that will never change again.
TERMINAL_STATUSES = frozenset(
    {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
)

#: Terminal statuses that are not a success.
FAILED_STATUSES = frozenset({"FAILED", "ABORTED", "TIMED-OUT"})

#: Statuses that mean the run is still going.
ACTIVE_STATUSES = frozenset(
    {"READY", "RUNNING", "ABORTING", "TIMING-OUT"}
)

#: Fields requested from the dataset. Unchanged from the previous
#: implementation so the Airtable mapping keeps working.
DATASET_FIELDS = (
    "legacyId,url,time,text,user,groupTitle,"
    "facebookUrl,inputUrl,likesCount,"
    "commentsCount,sharesCount,error,errorDescription"
)

Requester = Callable[..., Any]


class ApifyError(RuntimeError):
    """Raised when Apify cannot give us a usable run or dataset."""


class ApifyRunNotFinished(ApifyError):
    """Raised when a run did not reach a terminal state in time."""


class ApifyRunFailed(ApifyError):
    """Raised when a run finished in a non-success state."""


@dataclass
class ApifyRun:
    """One exact Apify run, as resolved from the API.

    Fields left as ``None`` were genuinely absent from the API response.
    Callers must not substitute a default — see ``scraper_runs`` for how
    missing values are written to Airtable (they are not).
    """

    id: str
    status: str
    dataset_id: str
    task_id: str | None = None
    actor_id: str | None = None
    key_value_store_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    cost_usd: float | None = None
    item_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def dataset_url(self) -> str:
        """The Apify console URL for this run's dataset."""
        if not self.dataset_id:
            return ""
        return f"{CONSOLE_BASE}/storage/datasets/{self.dataset_id}"

    @property
    def console_url(self) -> str:
        """The Apify console URL for the run itself."""
        if not self.id:
            return ""
        return f"{CONSOLE_BASE}/view/runs/{self.id}"

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _duration_seconds(payload: dict[str, Any]) -> float | None:
    """
    Derive the run duration in seconds.

    Prefers ``stats.runTimeSecs``, which Apify reports directly, and falls
    back to the difference between ``startedAt`` and ``finishedAt``. Returns
    None when neither is available rather than reporting zero.
    """
    stats = payload.get("stats")
    if isinstance(stats, dict):
        run_time = stats.get("runTimeSecs")
        if isinstance(run_time, (int, float)):
            return float(run_time)

    started = _parse_timestamp(payload.get("startedAt"))
    finished = _parse_timestamp(payload.get("finishedAt"))

    if started and finished:
        return max((finished - started).total_seconds(), 0.0)

    return None


def _cost_usd(payload: dict[str, Any]) -> float | None:
    """
    Read the run's total cost.

    ``usageTotalUsd`` is only present for API tokens that own the run. When it
    is missing the cost is genuinely unknown and stays None; the Airtable Cost
    column is left untouched rather than filled with a made-up figure.
    """
    value = payload.get("usageTotalUsd")

    if isinstance(value, (int, float)):
        return float(value)

    return None


def parse_run(payload: Any) -> ApifyRun:
    """
    Build an ``ApifyRun`` from an Apify run object.

    Accepts either the bare run object or the ``{"data": {...}}`` envelope the
    API returns.
    """
    if not isinstance(payload, dict):
        raise ApifyError("Apify returned an unexpected run payload.")

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    run_id = str(data.get("id") or "").strip()
    if not run_id:
        raise ApifyError("Apify run response did not include a run ID.")

    return ApifyRun(
        id=run_id,
        status=str(data.get("status") or "").strip().upper(),
        dataset_id=str(data.get("defaultDatasetId") or "").strip(),
        task_id=str(data.get("actorTaskId") or "").strip() or None,
        actor_id=str(data.get("actId") or "").strip() or None,
        key_value_store_id=(
            str(data.get("defaultKeyValueStoreId") or "").strip() or None
        ),
        started_at=str(data.get("startedAt") or "").strip() or None,
        finished_at=str(data.get("finishedAt") or "").strip() or None,
        duration_seconds=_duration_seconds(data),
        cost_usd=_cost_usd(data),
        raw=data,
    )


def _auth_headers(token: str) -> dict[str, str]:
    """
    Build the Apify auth header.

    The token goes in a header rather than a ``?token=`` query parameter so it
    can never end up in a logged URL or an exception message.
    """
    return {"Authorization": f"Bearer {token}"}


def start_task_run(
    requester: Requester,
    *,
    token: str,
    task_id: str,
) -> ApifyRun:
    """Start a fresh run of the configured task and return it immediately."""
    response = requester(
        "POST",
        f"{API_BASE}/actor-tasks/{task_id}/runs",
        headers=_auth_headers(token),
    )

    run = parse_run(response.json())
    print(f"Started Apify run {run.id} for task {task_id}.", flush=True)
    return run


def get_run(
    requester: Requester,
    *,
    token: str,
    run_id: str,
) -> ApifyRun:
    """Fetch one exact run by its ID."""
    response = requester(
        "GET",
        f"{API_BASE}/actor-runs/{run_id}",
        headers=_auth_headers(token),
    )

    return parse_run(response.json())


def get_last_successful_run(
    requester: Requester,
    *,
    token: str,
    task_id: str,
) -> ApifyRun:
    """
    Resolve the task's most recent successful run *as a run object*.

    This is the important difference from the old implementation: it returns
    the run — with its ID and its own ``defaultDatasetId`` — rather than
    anonymous dataset items. Whatever we import is attributable.
    """
    response = requester(
        "GET",
        f"{API_BASE}/actor-tasks/{task_id}/runs/last",
        headers=_auth_headers(token),
        params={"status": "SUCCEEDED"},
    )

    payload = response.json() or {}
    data = payload.get("data")

    if not isinstance(data, dict) or not data.get("id"):
        raise ApifyError(
            f"Apify task {task_id} has no successful runs to read. "
            f"Start one with RUN_APIFY_TASK=true, or pin an exact run with "
            f"APIFY_RUN_ID."
        )

    run = parse_run(data)
    print(
        f"Resolved last successful Apify run {run.id} "
        f"(finished {run.finished_at}).",
        flush=True,
    )
    return run


def wait_for_run(
    requester: Requester,
    *,
    token: str,
    run_id: str,
    timeout_seconds: int,
    poll_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ApifyRun:
    """
    Poll one exact run until it reaches a terminal state.

    Raises ``ApifyRunFailed`` for FAILED / ABORTED / TIMED-OUT and
    ``ApifyRunNotFinished`` when the timeout expires, so the caller can tell a
    broken scrape apart from a slow one.
    """
    interval = max(int(poll_seconds), 1)
    deadline = monotonic() + max(int(timeout_seconds), 1)

    while True:
        run = get_run(requester, token=token, run_id=run_id)

        if run.status == "SUCCEEDED":
            print(f"Apify run {run.id} succeeded.", flush=True)
            return run

        if run.status in FAILED_STATUSES:
            raise ApifyRunFailed(
                f"Apify run {run.id} finished with status {run.status}."
            )

        if monotonic() >= deadline:
            raise ApifyRunNotFinished(
                f"Apify run {run.id} did not finish within "
                f"{timeout_seconds}s (last status: {run.status})."
            )

        print(
            f"Apify run {run.id} status={run.status}; "
            f"checking again in {interval}s...",
            flush=True,
        )
        sleep(interval)


def fetch_dataset_items(
    requester: Requester,
    *,
    token: str,
    dataset_id: str,
    fields: str = DATASET_FIELDS,
) -> list[dict[str, Any]]:
    """Read items from one specific dataset, by ID."""
    if not dataset_id:
        raise ApifyError("Cannot fetch a dataset without a dataset ID.")

    response = requester(
        "GET",
        f"{API_BASE}/datasets/{dataset_id}/items",
        headers=_auth_headers(token),
        params={"format": "json", "clean": "true", "fields": fields},
    )

    data = response.json()
    if not isinstance(data, list):
        raise ApifyError(
            f"Apify dataset {dataset_id} returned an unexpected response."
        )

    return data


def fetch_run_input(
    requester: Requester,
    *,
    token: str,
    run: ApifyRun,
) -> Any:
    """
    Read the exact input the run was started with.

    Apify stores it as the ``INPUT`` record in the run's default key-value
    store. A missing record is normal for some actors, so this returns None
    rather than raising — the Run Input column is simply left blank.
    """
    if not run.key_value_store_id:
        return None

    try:
        response = requester(
            "GET",
            f"{API_BASE}/key-value-stores/"
            f"{run.key_value_store_id}/records/INPUT",
            headers=_auth_headers(token),
        )
    except Exception as exc:  # noqa: BLE001 - input is best-effort metadata
        print(
            f"Could not read the input for Apify run {run.id}: {exc}",
            flush=True,
        )
        return None

    try:
        return response.json()
    except ValueError:
        return None


def resolve_run(
    requester: Requester,
    *,
    token: str,
    task_id: str,
    start_new_run: bool = False,
    run_id: str = "",
    timeout_seconds: int = 900,
    poll_seconds: int = 15,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ApifyRun:
    """
    Resolve the single run this pipeline execution will import from.

    Three modes, in precedence order:

    1. ``run_id`` supplied (``APIFY_RUN_ID``) — use exactly that run. Useful
       for re-importing a specific scrape, and for reproducing a bad import.
    2. ``start_new_run`` (``RUN_APIFY_TASK=true``) — start the task, then poll
       *that run's ID* to completion. Never reads "the last run", so a
       concurrent manual run cannot be picked up by mistake.
    3. Otherwise — resolve the task's last successful run as a run object.

    In every mode the returned run carries its own ``dataset_id``. The caller
    fetches items from that dataset and nothing else.
    """
    if run_id:
        run = get_run(requester, token=token, run_id=run_id)
        print(f"Using pinned Apify run {run.id} (status {run.status}).", flush=True)

        if not run.is_terminal:
            run = wait_for_run(
                requester,
                token=token,
                run_id=run.id,
                timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds,
                sleep=sleep,
                monotonic=monotonic,
            )

        if not run.succeeded:
            raise ApifyRunFailed(
                f"Pinned Apify run {run.id} has status {run.status}."
            )

    elif start_new_run:
        started = start_task_run(requester, token=token, task_id=task_id)
        run = wait_for_run(
            requester,
            token=token,
            run_id=started.id,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            sleep=sleep,
            monotonic=monotonic,
        )

        # The polled response is authoritative, but keep the dataset ID from
        # the start response if the final payload somehow omitted it.
        if not run.dataset_id:
            run.dataset_id = started.dataset_id

    else:
        run = get_last_successful_run(requester, token=token, task_id=task_id)

    if not run.dataset_id:
        raise ApifyError(
            f"Apify run {run.id} did not expose a default dataset ID, so its "
            f"posts cannot be attributed to it."
        )

    return run


def collect_posts(
    requester: Requester,
    *,
    token: str,
    run: ApifyRun,
) -> list[dict[str, Any]]:
    """
    Fetch and validate the posts belonging to one exact run.

    Sets ``run.item_count`` to the raw dataset size (before validity
    filtering) so the Scraper Runs record reports what Apify actually
    returned.
    """
    items = fetch_dataset_items(
        requester, token=token, dataset_id=run.dataset_id
    )
    run.item_count = len(items)

    posts = [
        item
        for item in items
        if isinstance(item, dict) and item.get("url") and not item.get("error")
    ]

    print(
        f"Apify run {run.id} dataset {run.dataset_id}: "
        f"{len(items)} items, {len(posts)} valid posts.",
        flush=True,
    )
    return posts
