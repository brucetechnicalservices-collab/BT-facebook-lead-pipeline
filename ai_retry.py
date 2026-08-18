"""
Cross-run AI retry accounting.

A record whose AI call fails must be retryable on a later run, but not
forever. This module owns that bookkeeping — and, more importantly, owns the
single decision of *where the attempt count is stored*.

Why this module exists
----------------------

The live Airtable schema has no ``AI Attempts`` field, so the count is
currently kept as JSON inside ``AI Output``, which the schema does have. That
works, but it is a storage detail that should not be spread across the
pipeline: without isolation, adding a proper numeric field later would mean
touching the queue, the error handler, and the qualification wiring.

Everything that knows about the storage location lives here, behind
``RetryStore``. Migrating to a dedicated field is then a configuration change
rather than a code change:

    # today — no dedicated field exists
    store = RetryStore(output_field="AI Output")

    # after adding an "AI Attempts" number field in Airtable
    store = RetryStore(output_field="AI Output", attempts_field="AI Attempts")

Nothing in the qualification architecture changes either way. Scoring, tiers,
hard rejections, and outreach rules never consult retry state.

Storage semantics
-----------------

* With ``attempts_field`` set, the count is read from and written to that
  numeric field, and ``AI Output`` keeps only human-readable diagnostics.
* Without it, the count rides along in the ``AI Output`` JSON blob.
* Either way the count is written **only for records that actually failed**.
  Real model output is never replaced by exception text.
* An ``AI Output`` value that is not this JSON shape — legacy text, or real
  model output from an earlier success — reads as zero attempts. The record
  then gets a fresh budget rather than being wrongly retired. See
  docs/PIPELINE_AUDIT.md for the consequence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

#: Key holding the attempt count inside the AI Output JSON blob.
ATTEMPTS_KEY = "attempts"

#: Marks a blob as retry bookkeeping rather than model output.
TRANSIENT_KEY = "transient"

#: Exception text fragments that indicate a transient failure worth retrying.
TRANSIENT_ERROR_MARKERS = (
    "timeout", "timed out", "rate limit", "429", "500", "502", "503", "504",
    "connection", "temporarily", "overloaded", "unavailable", "network",
)


def is_transient_error(error: BaseException) -> bool:
    """
    Decide whether an AI failure is worth another attempt later.

    Transient: timeouts, rate limits, 5xx, connection resets. Permanent: a
    malformed record, an impossible schema state, a rejected prompt. A
    permanent failure burns the whole budget at once so it never comes back.
    """
    text = f"{type(error).__name__}: {error}".lower()

    return any(marker in text for marker in TRANSIENT_ERROR_MARKERS)


@dataclass(frozen=True)
class RetryStore:
    """
    Where retry state lives, and how to read and write it.

    ``output_field`` is the long-text field holding diagnostics.
    ``attempts_field`` is an optional dedicated numeric field; when set it
    becomes the source of truth for the count.
    """

    output_field: str
    attempts_field: str = ""
    error_status: str = "Error"

    @property
    def uses_dedicated_field(self) -> bool:
        """True once a real ``AI Attempts`` field is configured."""
        return bool(self.attempts_field)

    def read_attempts(self, fields: dict[str, Any] | None) -> int:
        """How many AI attempts this record has already used, across runs."""
        record_fields = fields or {}

        if self.attempts_field:
            raw = record_fields.get(self.attempts_field)
            if raw is not None and str(raw).strip():
                try:
                    return max(int(float(raw)), 0)
                except (TypeError, ValueError):
                    return 0

        return self._attempts_from_blob(record_fields.get(self.output_field))

    def _attempts_from_blob(self, raw: Any) -> int:
        if not raw:
            return 0

        try:
            parsed = json.loads(str(raw))
        except (ValueError, TypeError):
            return 0

        if not isinstance(parsed, dict):
            return 0

        try:
            return max(int(parsed.get(ATTEMPTS_KEY, 0)), 0)
        except (TypeError, ValueError):
            return 0

    def is_exhausted(
        self,
        fields: dict[str, Any] | None,
        *,
        max_attempts: int,
        status_field: str,
    ) -> bool:
        """
        Has this record used up its cross-run attempt budget?

        Only records currently marked ``Error`` are considered. A processed
        record carrying a stale count is never retired.
        """
        record_fields = fields or {}
        status = str(record_fields.get(status_field, "") or "").strip()

        if status != self.error_status:
            return False

        return self.read_attempts(record_fields) >= max_attempts

    def build_failure_fields(
        self,
        error_message: str,
        *,
        attempts: int,
        transient: bool,
        status_field: str,
        version: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Build the Airtable payload for a failed record.

        Deliberately narrow: status, diagnostics, and the attempt count. It
        never includes Lead Score, Qualified, or Suggested DM, so a failure
        cannot blank a record's existing lead data.
        """
        timestamp = (now or datetime.now(timezone.utc)).isoformat()

        diagnostics: dict[str, Any] = {
            "qualification_version": version,
            TRANSIENT_KEY: transient,
            "last_error": str(error_message)[:1000],
            "last_error_at": timestamp,
        }

        payload: dict[str, Any] = {status_field: self.error_status}

        if self.attempts_field:
            payload[self.attempts_field] = int(attempts)
        else:
            # No dedicated field yet: the count rides in the blob.
            diagnostics[ATTEMPTS_KEY] = int(attempts)

        payload[self.output_field] = json.dumps(
            diagnostics, indent=2, sort_keys=True
        )
        return payload

    def next_attempt_count(
        self,
        fields: dict[str, Any] | None,
        *,
        transient: bool,
        max_attempts: int,
    ) -> int:
        """
        The attempt number to record for a failure that just happened.

        A transient failure increments. A permanent one jumps straight to the
        ceiling so the record is never retried.
        """
        if not transient:
            return max_attempts

        return self.read_attempts(fields) + 1
