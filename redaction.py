"""
Log redaction for the BruceTech Facebook lead pipeline.

GitHub Actions logs for this repository are readable by anyone who can see
the repository, and the pipeline handles other people's Facebook posts. A run
on 2026-08-20 printed complete post URLs, author names, and full Airtable
record IDs for two hundred records, which is a copy of the source data in a
place nobody chose to put it.

Nothing here is an access control. It is a rule about what the pipeline is
allowed to say out loud: identifiers are replaced by short fingerprints, and
the operational facts an operator actually needs -- queue position, intent,
prefilter score, rejection codes, totals -- are printed in full.

The fingerprint is keyed with a salt that is random per process unless
LOG_FINGERPRINT_SALT is set, so a published log cannot be matched back to a
post by hashing a candidate URL. Set that variable to a fixed value when you
need to correlate the same record across two runs.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets


#: Length of a printed fingerprint. Eight hex characters is enough to tell
#: two hundred records apart in one log without inviting anyone to treat it
#: as an identifier.
FINGERPRINT_LENGTH = 8

#: Substituted for a value that is empty or missing.
EMPTY_MARKER = "-"

_ENV_SALT = "LOG_FINGERPRINT_SALT"

#: Random unless the operator pins it. Resolved once per process.
_SALT: bytes | None = None


def _salt() -> bytes:
    """Return the keying material for this process."""
    global _SALT

    if _SALT is None:
        configured = os.getenv(_ENV_SALT, "").strip()
        _SALT = (
            configured.encode("utf-8")
            if configured
            else secrets.token_bytes(16)
        )

    return _SALT


def reset_salt() -> None:
    """Forget the cached salt so the next call re-reads the environment."""
    global _SALT
    _SALT = None


def fingerprint(value: object, *, length: int = FINGERPRINT_LENGTH) -> str:
    """
    Short, keyed, non-reversible tag for one sensitive value.

    Equal inputs give equal output within a run, so two log lines about the
    same record can be matched up. Nothing about the input can be recovered,
    and without the salt a guessed input cannot be confirmed either.
    """
    text = str(value or "").strip()
    if not text:
        return EMPTY_MARKER

    return hashlib.blake2s(
        text.encode("utf-8", "replace"),
        key=_salt(),
        digest_size=16,
    ).hexdigest()[:length]


def redact_record_id(record_id: object) -> str:
    """Render an Airtable record ID as a fingerprint."""
    return f"rec:{fingerprint(record_id)}"


def redact_url(url: object) -> str:
    """Render a post URL as a fingerprint."""
    return f"post:{fingerprint(url)}"


def redact_author(author: object) -> str:
    """Render an author name as a fingerprint."""
    return f"author:{fingerprint(author)}"


# ---------------------------------------------------------------------------
# Defensive scrubbing
#
# The call sites below decide what to print. These patterns catch identifiers
# that arrive inside somebody else's string -- an exception message, an API
# error body -- where the call site cannot know what it is holding.
# ---------------------------------------------------------------------------

#: Any Facebook or fb.com URL, however it is spelled.
_FACEBOOK_URL_RE = re.compile(
    r"https?://[^\s\"'<>]*\b(?:facebook|fb)\.(?:com|me)[^\s\"'<>]*",
    re.IGNORECASE,
)

#: Airtable object IDs: a three-letter type prefix and fourteen characters.
#: Record IDs identify a post; base and table IDs are workflow secrets.
_AIRTABLE_ID_RE = re.compile(r"\b(rec|app|tbl)[A-Za-z0-9]{14}\b")

#: Synthetic IDs minted for dry-run records. They carry no post data, but
#: they read like record IDs and are scrubbed so the two stay consistent.
_DRY_RUN_ID_RE = re.compile(r"\bdryrun-[A-Za-z0-9_-]+")


def scrub(text: object, *, limit: int | None = None) -> str:
    """
    Replace identifiers inside an arbitrary string with fingerprints.

    Use this on anything the pipeline did not compose itself: exception
    messages, HTTP error bodies, third-party library output.
    """
    result = str(text or "")

    result = _FACEBOOK_URL_RE.sub(
        lambda match: redact_url(match.group(0)), result
    )
    result = _AIRTABLE_ID_RE.sub(
        lambda match: f"{match.group(1)}:{fingerprint(match.group(0))}",
        result,
    )
    result = _DRY_RUN_ID_RE.sub(
        lambda match: redact_record_id(match.group(0)), result
    )

    if limit is not None and len(result) > limit:
        result = result[:limit] + "..."

    return result
