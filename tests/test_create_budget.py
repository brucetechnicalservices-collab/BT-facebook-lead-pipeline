"""
Regression tests for AIRTABLE_LEAD_CREATE_BUDGET.

GitHub Actions dry run 32658122909 read Apify run IPOHKRCznDcvYgnyx, dataset
KFiSxm6fiXVSAY0kK: 217 items, 214 new unique leads, 3 duplicates. It wrote
nothing because it was a dry run. A write-enabled version of exactly that run
would have created 214 Facebook Raw Signal rows, and nothing anywhere in the
pipeline would have stopped it.

AIRTABLE_LEAD_UPDATE_BUDGET does not cover this. It bounds changes to records
the base already holds; creation is a different path and a different risk.
This is the ceiling for creation, and these tests hold its contract:

* it counts records, not requests or batches;
* it is claimed before a post is added to a create batch, so a deferred post
  is never mapped and never POSTed;
* 0 or blank is unlimited, which is what production has always done;
* a dry run neither spends it nor is limited by it;
* a deferred post is not lost -- the next run of the same Apify dataset
  deduplicates against what landed and imports the next batch, with no
  duplicates and no gaps.
"""

from __future__ import annotations

import pytest
import yaml

import run_pipeline as rp
from normalization import identity_from_apify

APIFY_RUN_ID = "IPOHKRCznDcvYgnyx"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class CreateRecorder:
    """
    Stands in for request_with_retry and counts real record creations.

    Counting at the HTTP layer is the only way to prove batching is not
    hiding anything: ten records in one POST must count as ten.
    """

    def __init__(self):
        self.created_fields: list[dict] = []
        self.calls = 0

    def __call__(self, method, url, **kwargs):
        self.calls += 1
        payload = (kwargs.get("json") or {}).get("records", [])
        self.created_fields.extend(
            record.get("fields", {}) for record in payload
        )
        return _FakeResponse(
            {
                "records": [
                    {"id": f"recCREATED{index:07d}", "fields": {}}
                    for index in range(
                        len(self.created_fields) - len(payload),
                        len(self.created_fields),
                    )
                ]
            }
        )

    @property
    def created_urls(self) -> list[str]:
        return [f.get(rp.FIELD_URL, "") for f in self.created_fields]


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def post(index: int) -> dict:
    """One Apify dataset item, distinct by URL, id, text, and author."""
    return {
        "postId": f"post-{index:05d}",
        "legacyId": f"legacy-{index:05d}",
        "url": f"https://www.facebook.com/groups/998877/posts/{index:09d}/",
        "text": f"Distinct post body number {index} about a business need.",
        "time": "2026-08-23T12:00:00.000Z",
        "user": {"id": f"u{index}", "name": f"Author {index}"},
    }


def dataset(size: int) -> list[dict]:
    return [post(index) for index in range(1, size + 1)]


@pytest.fixture(autouse=True)
def clean_budget():
    rp.reset_lead_create_budget()
    yield
    rp.reset_lead_create_budget()


@pytest.fixture
def airtable_env(monkeypatch):
    """Fake configuration. No request ever leaves the process."""
    monkeypatch.setenv("AIRTABLE_TOKEN", "fake-token-not-a-secret")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appTESTTESTTEST0")
    monkeypatch.setenv("AIRTABLE_TABLE_NAME", "Facebook Raw Signals")
    return monkeypatch


def run_import(
    monkeypatch,
    posts,
    *,
    budget=0,
    dry_run=False,
    recorder=None,
    summary=None,
):
    """Drive the real create path. Returns (created_ids, recorder, summary)."""
    recorder = recorder or CreateRecorder()
    summary = summary if summary is not None else rp.RunSummary()

    monkeypatch.setattr(rp, "AIRTABLE_LEAD_CREATE_BUDGET", budget)
    monkeypatch.setattr(rp, "DRY_RUN", dry_run)
    monkeypatch.setattr(rp.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(rp, "request_with_retry", recorder)

    created = rp.create_new_posts_in_airtable(
        posts, apify_run_id=APIFY_RUN_ID, summary=summary
    )

    return created, recorder, summary


# ===========================================================================
# Budget 0 -- unlimited, the production default
# ===========================================================================

def test_the_application_default_is_unlimited():
    assert rp.DEFAULT_AIRTABLE_LEAD_CREATE_BUDGET == 0


def test_a_blank_budget_is_unlimited(monkeypatch):
    """The workflow sends "" when the operator leaves the box alone."""
    monkeypatch.setenv("AIRTABLE_LEAD_CREATE_BUDGET", "")

    assert rp.env_int(
        "AIRTABLE_LEAD_CREATE_BUDGET",
        rp.DEFAULT_AIRTABLE_LEAD_CREATE_BUDGET,
    ) == 0


def test_budget_zero_creates_every_new_post(monkeypatch, airtable_env):
    """214 new posts, the size of the real 2026-08-23 scrape."""
    created, recorder, summary = run_import(
        monkeypatch, dataset(214), budget=0
    )

    assert len(created) == 214
    assert len(recorder.created_fields) == 214
    assert summary.creation_candidates == 214
    assert summary.creations_written == 214
    assert summary.creations_deferred == 0
    assert summary.lead_create_budget_exhausted is False


def test_budget_zero_leaves_the_helpers_unlimited(monkeypatch):
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_CREATE_BUDGET", 0)
    monkeypatch.setattr(rp, "DRY_RUN", False)

    assert rp.lead_create_budget_remaining() is None
    assert rp.has_lead_create_budget(10_000) is True


# ===========================================================================
# Budget 1 and 3
# ===========================================================================

def test_budget_one_creates_exactly_one_record(monkeypatch, airtable_env):
    created, recorder, summary = run_import(
        monkeypatch, dataset(214), budget=1
    )

    assert len(created) == 1
    assert len(recorder.created_fields) == 1
    assert summary.creations_written == 1
    assert summary.creations_deferred == 213
    assert summary.lead_create_budget_exhausted is True


def test_budget_three_creates_exactly_three_records(
    monkeypatch, airtable_env
):
    created, recorder, summary = run_import(
        monkeypatch, dataset(214), budget=3
    )

    assert len(created) == 3
    assert len(recorder.created_fields) == 3
    assert rp.lead_creates_made() == 3
    assert summary.creations_written == 3
    assert summary.creations_deferred == 211


def test_the_counters_account_for_every_candidate(monkeypatch, airtable_env):
    _, _, summary = run_import(monkeypatch, dataset(214), budget=3)

    assert summary.creations_written + summary.creations_deferred == (
        summary.creation_candidates
    )


def test_a_budget_larger_than_the_scrape_defers_nothing(
    monkeypatch, airtable_env
):
    created, _, summary = run_import(monkeypatch, dataset(5), budget=50)

    assert len(created) == 5
    assert summary.creations_deferred == 0
    assert summary.lead_create_budget_exhausted is False


def test_consuming_past_the_budget_raises(monkeypatch):
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_CREATE_BUDGET", 2)
    monkeypatch.setattr(rp, "DRY_RUN", False)

    rp.consume_lead_create()
    rp.consume_lead_create()

    with pytest.raises(rp.LeadCreateBudgetExhausted):
        rp.consume_lead_create()


def test_the_exhaustion_message_says_the_posts_stay_importable(monkeypatch):
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_CREATE_BUDGET", 1)
    monkeypatch.setattr(rp, "DRY_RUN", False)
    rp.consume_lead_create()

    with pytest.raises(rp.LeadCreateBudgetExhausted) as caught:
        rp.consume_lead_create()

    message = str(caught.value)
    assert "AIRTABLE_LEAD_CREATE_BUDGET" in message
    assert "importable" in message
    assert "deduplicates" in message


# ===========================================================================
# Records, not requests or batches
# ===========================================================================

def test_the_budget_counts_records_not_http_requests(
    monkeypatch, airtable_env
):
    """
    Creates flush in batches of ten. A budget of 25 must mean 25 records,
    which is three POSTs, not 25.
    """
    created, recorder, _ = run_import(
        monkeypatch, dataset(214), budget=25
    )

    assert len(created) == 25
    assert len(recorder.created_fields) == 25
    assert recorder.calls == 3


def test_an_unbudgeted_run_still_batches_in_tens(monkeypatch, airtable_env):
    _, recorder, _ = run_import(monkeypatch, dataset(35), budget=0)

    assert len(recorder.created_fields) == 35
    assert recorder.calls == 4


def test_a_deferred_post_is_never_mapped_or_sent(monkeypatch, airtable_env):
    """
    The budget is claimed before the create batch is built, so a deferred
    post must not appear in any payload at all.
    """
    posts = dataset(10)
    _, recorder, _ = run_import(monkeypatch, posts, budget=3)

    sent = set(recorder.created_urls)
    deferred_urls = {
        rp.normalize_facebook_url(p["url"]) for p in posts[3:]
    }

    assert len(sent) == 3
    assert sent.isdisjoint(deferred_urls)


def test_the_deferred_split_is_a_contiguous_tail(monkeypatch, airtable_env):
    """Order is preserved, so the operator can reason about what landed."""
    posts = dataset(10)
    _, recorder, _ = run_import(monkeypatch, posts, budget=4)

    expected = [rp.normalize_facebook_url(p["url"]) for p in posts[:4]]

    assert recorder.created_urls == expected


# ===========================================================================
# Dry run
# ===========================================================================

def test_a_dry_run_creates_nothing_and_spends_nothing(monkeypatch):
    created, recorder, summary = run_import(
        monkeypatch, dataset(214), budget=3, dry_run=True
    )

    assert created == []
    assert recorder.calls == 0
    assert recorder.created_fields == []
    assert rp.lead_creates_made() == 0


def test_a_dry_run_reports_the_whole_scrape_not_the_budget(monkeypatch):
    """
    Run 32658122909 is informative because it said 214. A dry run that
    silently reported a budgeted 3 would hide the size of the problem.
    """
    _, _, summary = run_import(
        monkeypatch, dataset(214), budget=3, dry_run=True
    )

    assert summary.creation_candidates == 214
    assert summary.creations_written == 214, "simulated, not written"
    assert summary.creations_deferred == 0
    assert summary.lead_create_budget_exhausted is False


def test_the_dry_run_helpers_report_unlimited(monkeypatch):
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_CREATE_BUDGET", 3)
    monkeypatch.setattr(rp, "DRY_RUN", True)

    assert rp.lead_create_budget_remaining() is None
    assert rp.has_lead_create_budget(10_000) is True


def test_the_dry_run_summary_says_the_creations_were_simulated(monkeypatch):
    _, _, summary = run_import(
        monkeypatch, dataset(4), budget=3, dry_run=True
    )
    monkeypatch.setattr(rp, "DRY_RUN", True)

    rendered = summary.render()

    assert "Creations simulated" in rendered
    assert "nothing written" in rendered.lower()
    assert "not spent (dry run)" in rendered


def test_a_dry_run_never_leaks_a_post_url(monkeypatch, capsys):
    run_import(monkeypatch, dataset(12), budget=3, dry_run=True)

    output = capsys.readouterr().out

    assert "facebook.com" not in output
    assert "post:" in output


# ===========================================================================
# Deferred posts stay importable, with no duplicates
# ===========================================================================

def seen_keys_for(posts) -> set[str]:
    """The identity keys Airtable would hold after these posts landed."""
    keys: set[str] = set()
    for item in posts:
        keys.update(identity_from_apify(item).keys())
    return keys


def test_deferred_posts_are_new_again_on_the_next_run(monkeypatch):
    """
    The property that makes deferring safe: deduplication is by post
    identity against the live base, so an uncreated post is simply new.
    """
    posts = dataset(10)
    landed = posts[:3]

    remaining = rp.select_new_posts(posts, seen_keys_for(landed))

    assert len(remaining) == 7
    assert [p["postId"] for p in remaining] == [
        p["postId"] for p in posts[3:]
    ]


def test_a_second_run_of_the_same_dataset_imports_the_next_batch(
    monkeypatch, airtable_env
):
    """
    Three runs of one pinned Apify dataset with a budget of 3, each
    deduplicating against what the previous runs created. Ten posts drain in
    four runs with no duplicates and no gaps.
    """
    posts = dataset(10)
    landed: list[dict] = []
    runs = []

    for _ in range(4):
        rp.reset_lead_create_budget()
        new_posts = rp.select_new_posts(posts, seen_keys_for(landed))
        created, recorder, _ = run_import(
            monkeypatch, new_posts, budget=3
        )
        runs.append(len(created))
        landed.extend(
            p
            for p in new_posts[: len(created)]
        )

    assert runs == [3, 3, 3, 1]
    assert len(landed) == 10

    ids = [p["postId"] for p in landed]
    assert len(set(ids)) == 10, "no post imported twice"
    assert set(ids) == {p["postId"] for p in posts}, "no post left behind"


def test_a_fourth_run_finds_nothing_left_to_import(monkeypatch):
    posts = dataset(10)

    remaining = rp.select_new_posts(posts, seen_keys_for(posts))

    assert remaining == []


def test_a_repeat_run_creates_no_duplicate_when_everything_landed(
    monkeypatch, airtable_env
):
    posts = dataset(5)
    new_posts = rp.select_new_posts(posts, seen_keys_for(posts))

    created, recorder, summary = run_import(
        monkeypatch, new_posts, budget=3
    )

    assert created == []
    assert recorder.calls == 0
    assert summary.creations_written == 0
    assert summary.creations_deferred == 0


# ===========================================================================
# Separation from the update budget
# ===========================================================================

def test_the_two_budgets_are_separate_settings():
    assert rp.AIRTABLE_LEAD_CREATE_BUDGET is not None
    assert rp.AIRTABLE_LEAD_UPDATE_BUDGET is not None
    assert (
        rp.DEFAULT_AIRTABLE_LEAD_CREATE_BUDGET
        == rp.DEFAULT_AIRTABLE_LEAD_UPDATE_BUDGET
        == 0
    ), "both default to unlimited, but they are read from different names"


def test_creating_records_does_not_spend_the_update_budget(
    monkeypatch, airtable_env
):
    rp.reset_lead_update_budget()
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_UPDATE_BUDGET", 3)

    run_import(monkeypatch, dataset(10), budget=0)

    assert rp.lead_updates_made() == 0
    assert rp.lead_creates_made() == 10


def test_the_create_budget_has_its_own_counter(monkeypatch):
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_CREATE_BUDGET", 5)
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_UPDATE_BUDGET", 5)
    monkeypatch.setattr(rp, "DRY_RUN", False)
    rp.reset_lead_update_budget()

    rp.consume_lead_create(2)

    assert rp.lead_creates_made() == 2
    assert rp.lead_updates_made() == 0
    assert rp.lead_create_budget_remaining() == 3
    assert rp.lead_update_budget_remaining() == 5


# ===========================================================================
# Workflow wiring
# ===========================================================================

WORKFLOW = ".github/workflows/facebook-leads.yml"


@pytest.fixture(scope="module")
def workflow():
    """
    The parsed pipeline workflow.

    yaml is imported at module scope, never with importorskip: a runner
    without PyYAML must fail collection rather than skip these silently.
    See tests/test_validation_safety.py for why that rule exists.
    """
    with open(WORKFLOW, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _dispatch_inputs(workflow):
    return (workflow.get("on") or workflow.get(True))["workflow_dispatch"][
        "inputs"
    ]


def _pipeline_env(workflow):
    steps = workflow["jobs"]["run-pipeline"]["steps"]
    return next(step for step in steps if "env" in step)["env"]


def test_the_workflow_offers_the_create_budget_as_an_input(workflow):
    assert "airtable_lead_create_budget" in _dispatch_inputs(workflow)


def test_the_create_budget_input_does_not_default_to_unlimited(workflow):
    """
    Changed deliberately during the 2026-08-23 refinement. This used to
    assert the input defaulted to blank, which the application reads as
    unlimited -- so dispatching the workflow without touching the field
    could create every post in the dataset. The default is now 3 and the
    field is required; see the block below for the full contract.
    """
    default = _dispatch_inputs(workflow)["airtable_lead_create_budget"][
        "default"
    ]

    assert default != ""
    assert int(default) > 0


def test_the_create_budget_input_says_it_counts_new_records(workflow):
    description = _dispatch_inputs(workflow)["airtable_lead_create_budget"][
        "description"
    ].lower()

    assert "new" in description
    assert "unlimited" in description
    assert "importable" in description


def test_the_workflow_passes_the_create_budget_to_the_pipeline(workflow):
    env = _pipeline_env(workflow)

    assert "AIRTABLE_LEAD_CREATE_BUDGET" in env
    assert "github.event.inputs.airtable_lead_create_budget" in (
        env["AIRTABLE_LEAD_CREATE_BUDGET"]
    )


def test_the_two_budget_inputs_are_distinct(workflow):
    env = _pipeline_env(workflow)

    assert (
        env["AIRTABLE_LEAD_CREATE_BUDGET"]
        != env["AIRTABLE_LEAD_UPDATE_BUDGET"]
    )
    assert "lead_create_budget" not in env["AIRTABLE_LEAD_UPDATE_BUDGET"]
    assert "lead_update_budget" not in env["AIRTABLE_LEAD_CREATE_BUDGET"]


# ===========================================================================
# The workflow default is deliberately NOT the application default
#
# run_pipeline defaults to 0 (unlimited) for backward compatibility: an
# existing deployment that sets nothing keeps behaving exactly as it did. A
# manual dispatch defaults to 3, because clicking "Run workflow" without
# touching the field must not be able to create 214 rows. Unlimited creation
# has to be typed.
# ===========================================================================

WORKFLOW_CREATE_BUDGET_DEFAULT = "3"


def test_the_create_budget_input_is_required(workflow):
    assert _dispatch_inputs(workflow)["airtable_lead_create_budget"][
        "required"
    ] is True


def test_the_create_budget_input_defaults_to_three(workflow):
    assert _dispatch_inputs(workflow)["airtable_lead_create_budget"][
        "default"
    ] == WORKFLOW_CREATE_BUDGET_DEFAULT


def test_the_workflow_default_is_a_safe_positive_number(workflow):
    default = int(
        _dispatch_inputs(workflow)["airtable_lead_create_budget"]["default"]
    )

    assert default > 0, "a dispatched run must be capped, not unlimited"
    assert default != rp.DEFAULT_AIRTABLE_LEAD_CREATE_BUDGET, (
        "the whole point is that the workflow is stricter than the "
        "application default"
    )


def test_an_omitted_workflow_override_resolves_to_three(workflow):
    """
    The env fallback carries the same 3, so the value is never empty even if
    this workflow is ever triggered by something that supplies no inputs.
    """
    expression = _pipeline_env(workflow)["AIRTABLE_LEAD_CREATE_BUDGET"]

    assert f"'{WORKFLOW_CREATE_BUDGET_DEFAULT}'" in expression
    assert "|| ''" not in expression, "an empty fallback would be unlimited"


def test_the_create_budget_input_tells_the_operator_how_to_go_unlimited(
    workflow,
):
    description = _dispatch_inputs(workflow)["airtable_lead_create_budget"][
        "description"
    ].lower()

    assert "0" in description
    assert "unlimited" in description


def test_only_the_create_budget_input_is_required(workflow):
    """
    Marking every input required would make the form hostile. Exactly one is
    required, because exactly one has a default that must be seen.
    """
    required = {
        name
        for name, spec in _dispatch_inputs(workflow).items()
        if spec.get("required")
    }

    assert required == {"airtable_lead_create_budget"}


def test_the_application_default_is_still_unlimited_for_compatibility():
    """
    Backward compatibility: a deployment that sets nothing must behave
    exactly as it did before this budget existed.
    """
    assert rp.DEFAULT_AIRTABLE_LEAD_CREATE_BUDGET == 0
    assert rp.env_int(
        "AIRTABLE_LEAD_CREATE_BUDGET",
        rp.DEFAULT_AIRTABLE_LEAD_CREATE_BUDGET,
    ) == 0


def test_an_explicit_zero_still_means_unlimited(monkeypatch, airtable_env):
    """Typing 0 in the now-required field restores unlimited creation."""
    monkeypatch.setenv("AIRTABLE_LEAD_CREATE_BUDGET", "0")
    resolved = rp.env_int(
        "AIRTABLE_LEAD_CREATE_BUDGET",
        rp.DEFAULT_AIRTABLE_LEAD_CREATE_BUDGET,
    )

    assert resolved == 0

    created, recorder, summary = run_import(
        monkeypatch, dataset(214), budget=resolved
    )

    assert len(created) == 214
    assert len(recorder.created_fields) == 214
    assert summary.creations_deferred == 0


def test_the_workflow_default_actually_caps_creation(
    monkeypatch, airtable_env
):
    """End to end: the string the workflow sends produces a 3-record cap."""
    monkeypatch.setenv(
        "AIRTABLE_LEAD_CREATE_BUDGET", WORKFLOW_CREATE_BUDGET_DEFAULT
    )
    resolved = rp.env_int(
        "AIRTABLE_LEAD_CREATE_BUDGET",
        rp.DEFAULT_AIRTABLE_LEAD_CREATE_BUDGET,
    )

    created, recorder, summary = run_import(
        monkeypatch, dataset(214), budget=resolved
    )

    assert resolved == 3
    assert len(recorder.created_fields) == 3
    assert summary.creations_deferred == 211


# ===========================================================================
# A negative budget is a configuration error, not a silent no-op
# ===========================================================================

def test_a_negative_create_budget_is_rejected(monkeypatch):
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_CREATE_BUDGET", -5)

    with pytest.raises(rp.ConfigurationError) as caught:
        rp.validate_budget_configuration()

    message = str(caught.value)
    assert "AIRTABLE_LEAD_CREATE_BUDGET=-5" in message
    assert "cannot be negative" in message
    assert "0" in message, "the message must say how to get unlimited"


def test_a_negative_update_budget_is_rejected(monkeypatch):
    """The sibling ceiling has the identical failure mode."""
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_UPDATE_BUDGET", -1)

    with pytest.raises(rp.ConfigurationError):
        rp.validate_budget_configuration()


def test_zero_and_positive_budgets_are_accepted(monkeypatch):
    for value in (0, 1, 3, 214):
        monkeypatch.setattr(rp, "AIRTABLE_LEAD_CREATE_BUDGET", value)
        monkeypatch.setattr(rp, "AIRTABLE_LEAD_UPDATE_BUDGET", value)
        rp.validate_budget_configuration()


def test_a_configuration_error_is_a_runtime_error():
    """So an existing `except RuntimeError` handler still catches it."""
    assert issubclass(rp.ConfigurationError, RuntimeError)


def test_a_negative_budget_fails_before_any_external_operation(monkeypatch):
    """
    The important half. A typo must cost nothing: no Airtable read, no Apify
    run resolution, no OpenAI client, no write of any kind.
    """
    touched: list[str] = []

    def forbid(name):
        def boom(*args, **kwargs):
            touched.append(name)
            raise AssertionError(f"{name} must not run on a bad config")
        return boom

    monkeypatch.setattr(rp, "AIRTABLE_LEAD_CREATE_BUDGET", -3)
    for name in (
        "validate_airtable_schema",
        "resolve_apify_run",
        "collect_apify_posts",
        "fetch_existing_identity_keys",
        "list_airtable_records",
        "create_table_records",
        "update_table_records",
        "update_airtable_records",
        "get_openai_client",
        "request_with_retry",
        "log_scraper_run",
        "refresh_group_performance",
    ):
        monkeypatch.setattr(rp, name, forbid(name))

    with pytest.raises(rp.ConfigurationError):
        rp.main()

    assert touched == []


def test_main_validates_before_it_prints_its_banner(monkeypatch, capsys):
    """Nothing at all happens first, not even the startup log line."""
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_CREATE_BUDGET", -1)

    with pytest.raises(rp.ConfigurationError):
        rp.main()

    assert capsys.readouterr().out == ""


def test_a_negative_budget_does_not_silently_defer_everything(
    monkeypatch, airtable_env
):
    """
    The behaviour being replaced. Before this check, -5 made the first claim
    fail, so every post deferred and the run reported success having written
    nothing. That must now be impossible to reach through main().
    """
    monkeypatch.setattr(rp, "AIRTABLE_LEAD_CREATE_BUDGET", -5)

    with pytest.raises(rp.ConfigurationError):
        rp.validate_budget_configuration()


def test_the_schedule_is_still_disabled(workflow):
    """These workflow edits must not have woken the daily run."""
    triggers = workflow.get("on") or workflow.get(True)

    assert "schedule" not in triggers


def test_the_schedule_is_still_only_a_comment():
    import re

    with open(WORKFLOW, encoding="utf-8") as handle:
        text = handle.read()

    assert "# schedule:" in text
    assert re.search(r"^\s*schedule:", text, re.MULTILINE) is None


def test_the_pipeline_entry_point_is_unchanged(workflow):
    """The job still runs the real pipeline, not a harness."""
    steps = workflow["jobs"]["run-pipeline"]["steps"]
    commands = [step.get("run", "") for step in steps]

    assert "python run_pipeline.py" in commands


def test_the_test_job_is_unchanged(workflow):
    steps = workflow["jobs"]["test"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)

    assert "compileall" in commands
    assert "pytest" in commands


def test_the_readme_documents_the_create_budget():
    with open("README.md", encoding="utf-8") as handle:
        readme = handle.read()

    _, _, reference = readme.partition("\n## Environment variables")
    assert reference, "README has no Environment variables section"

    row = next(
        line
        for line in reference.splitlines()
        if line.startswith("| `AIRTABLE_LEAD_CREATE_BUDGET` |")
    )

    assert f"`{rp.DEFAULT_AIRTABLE_LEAD_CREATE_BUDGET}`" in row
    assert "unlimited" in row.lower()
