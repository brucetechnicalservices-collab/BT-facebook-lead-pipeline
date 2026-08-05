"""
Facebook URL normalisation, text fingerprinting, and deduplication.

Airtable previously deduplicated on the raw ``Url`` string alone, so the same
post arriving with different tracking parameters, a different host
(``m.facebook.com`` vs ``www.facebook.com``), or a trailing slash was imported
more than once. This module derives stable identities instead.

Like ``qualification``, this module has no environment or network
dependencies so it is trivially testable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Hosts that all serve the same Facebook content.
FACEBOOK_HOSTS = frozenset(
    {
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "web.facebook.com",
        "mbasic.facebook.com",
        "touch.facebook.com",
        "free.facebook.com",
        "l.facebook.com",
        "fb.com",
        "www.fb.com",
    }
)

CANONICAL_FACEBOOK_HOST = "www.facebook.com"

#: Query parameters that carry tracking noise rather than identity.
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "__cft__",
        "__tn__",
        "__xts__",
        "__so__",
        "__req",
        "ref",
        "refid",
        "referrer",
        "source",
        "notif_id",
        "notif_t",
        "hc_ref",
        "hc_location",
        "rdid",
        "share_url",
        "mibextid",
        "extid",
        "paipv",
        "eav",
        "_rdr",
        "sfnsn",
        "idorvanity",
        "anchor_composer",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
    }
)

#: Query parameters that genuinely identify a post.
IDENTITY_PARAMS = ("story_fbid", "fbid", "id", "v", "post_id", "comment_id")

#: Patterns that yield a post identifier from a Facebook URL path.
_POST_ID_PATTERNS = (
    re.compile(r"/groups/[^/]+/(?:posts|permalink)/([A-Za-z0-9]+)"),
    re.compile(r"/posts/([A-Za-z0-9]+)"),
    re.compile(r"/permalink/([A-Za-z0-9]+)"),
    re.compile(r"/videos/([A-Za-z0-9]+)"),
    re.compile(r"/photos/[^/]+/([A-Za-z0-9]+)"),
    re.compile(r"/reel/([A-Za-z0-9]+)"),
    re.compile(r"/story\.php.*[?&]story_fbid=([A-Za-z0-9]+)"),
)

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")

#: Fingerprints shorter than this are treated as unreliable.
MIN_FINGERPRINT_TEXT_LENGTH = 25

FINGERPRINT_LENGTH = 32


def _tracking_param_base(key: str) -> str:
    """
    Reduce a query key to the name used for tracking-parameter matching.

    Facebook indexes several of these, so the raw key arrives as
    ``__cft__[0]`` rather than ``__cft__``.
    """
    return key.split("[", 1)[0].strip().lower()


def _strip_tracking_params(query: str) -> str:
    """Drop tracking parameters and sort what remains for stability."""
    if not query:
        return ""

    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=False)
        if _tracking_param_base(key) not in TRACKING_PARAMS
    ]

    if not kept:
        return ""

    kept.sort()
    return urlencode(kept)


def normalize_facebook_url(url: Any) -> str:
    """
    Return a canonical form of a Facebook URL.

    Normalises scheme and host, removes tracking parameters, and strips a
    trailing slash so that cosmetically different URLs for the same post
    compare equal.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""

    if raw.startswith("//"):
        raw = "https:" + raw
    elif not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw

    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.rstrip("/")

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        bare_host = host[4:]
    else:
        bare_host = host

    if host in FACEBOOK_HOSTS or bare_host == "facebook.com":
        host = CANONICAL_FACEBOOK_HOST

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    query = _strip_tracking_params(parts.query)

    return urlunsplit(("https", host, path, query, ""))


def extract_post_id(url: Any, legacy_id: Any = None) -> str:
    """
    Return the most stable available identifier for a post.

    Prefers the Apify ``legacyId`` because it is the platform's own ID, then
    falls back to parsing the URL path and identity query parameters.
    """
    legacy = str(legacy_id or "").strip()
    if legacy:
        return legacy

    raw = str(url or "").strip()
    if not raw:
        return ""

    normalized = normalize_facebook_url(raw)

    try:
        parts = urlsplit(normalized)
    except ValueError:
        return ""

    for pattern in _POST_ID_PATTERNS:
        match = pattern.search(parts.path)
        if match:
            return match.group(1)

    params = dict(parse_qsl(parts.query, keep_blank_values=False))
    for key in ("story_fbid", "post_id", "fbid", "v"):
        value = str(params.get(key, "")).strip()
        if value:
            return value

    return ""


def text_fingerprint(text: Any) -> str:
    """
    Return a stable fingerprint of post text.

    Case, punctuation, and whitespace are ignored so that a repost with
    trivial edits still collides. Returns an empty string when the text is
    too short to fingerprint reliably.
    """
    body = str(text or "").strip().lower()
    if not body:
        return ""

    body = _NON_ALNUM_RE.sub(" ", body)
    body = _WHITESPACE_RE.sub(" ", body).strip()

    if len(body) < MIN_FINGERPRINT_TEXT_LENGTH:
        return ""

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


@dataclass(frozen=True)
class PostIdentity:
    """The set of keys that can identify a post."""

    post_id: str = ""
    canonical_url: str = ""
    fingerprint: str = ""
    author: str = ""

    def keys(self) -> list[str]:
        """
        Return every dedupe key, strongest first.

        The fingerprint is scoped by author when an author is known, so two
        different businesses posting identical boilerplate are not merged.
        """
        keys: list[str] = []

        if self.post_id:
            keys.append(f"id:{self.post_id}")

        if self.canonical_url:
            keys.append(f"url:{self.canonical_url}")

        if self.fingerprint:
            if self.author:
                keys.append(f"fp:{self.author}:{self.fingerprint}")
            else:
                keys.append(f"fp:{self.fingerprint}")

        return keys


def build_identity(
    *,
    url: Any = None,
    legacy_id: Any = None,
    text: Any = None,
    author: Any = None,
) -> PostIdentity:
    """Build a PostIdentity from whatever fields are available."""
    author_name = str(author or "").strip().lower()

    return PostIdentity(
        post_id=extract_post_id(url, legacy_id),
        canonical_url=normalize_facebook_url(url),
        fingerprint=text_fingerprint(text),
        author=author_name,
    )


def identity_from_apify(post: dict[str, Any]) -> PostIdentity:
    """Build a PostIdentity from a raw Apify dataset item."""
    user = post.get("user") or {}

    return build_identity(
        url=post.get("url"),
        legacy_id=post.get("legacyId"),
        text=post.get("text"),
        author=user.get("name") or user.get("id"),
    )


def is_duplicate(identity: PostIdentity, seen_keys: set[str]) -> bool:
    """Return True when any of this post's keys has already been seen."""
    return any(key in seen_keys for key in identity.keys())


@dataclass
class DeduplicationResult:
    """Outcome of deduplicating a batch of posts."""

    unique: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicates)


def deduplicate_posts(
    posts: Iterable[dict[str, Any]],
    seen_keys: set[str] | None = None,
) -> DeduplicationResult:
    """
    Split posts into unique and duplicate sets.

    ``seen_keys`` should contain keys already present in Airtable. It is
    mutated as posts are accepted so duplicates inside the incoming batch are
    caught too.
    """
    keys = seen_keys if seen_keys is not None else set()
    result = DeduplicationResult()

    for post in posts:
        identity = identity_from_apify(post)
        post_keys = identity.keys()

        if not post_keys:
            # Nothing identifies this post; skip rather than risk a duplicate.
            result.duplicates.append(post)
            continue

        if is_duplicate(identity, keys):
            result.duplicates.append(post)
            continue

        keys.update(post_keys)
        result.unique.append(post)

    return result


def build_seen_keys(
    records: Iterable[dict[str, Any]],
    *,
    url_field: str,
    text_field: str | None = None,
    author_field: str | None = None,
) -> set[str]:
    """Build the seen-key set from existing Airtable records."""
    keys: set[str] = set()

    for record in records:
        fields = record.get("fields", {}) or {}

        identity = build_identity(
            url=fields.get(url_field),
            text=fields.get(text_field) if text_field else None,
            author=fields.get(author_field) if author_field else None,
        )
        keys.update(identity.keys())

    return keys
