"""Tests for Facebook URL normalisation, fingerprinting, and deduplication."""

from __future__ import annotations

import pytest

from normalization import (
    build_identity,
    build_seen_keys,
    deduplicate_posts,
    extract_post_id,
    is_duplicate,
    normalize_facebook_url,
    text_fingerprint,
)

CANONICAL = "https://www.facebook.com/groups/123456/posts/789012"

URL_VARIANTS = [
    "https://www.facebook.com/groups/123456/posts/789012",
    "https://www.facebook.com/groups/123456/posts/789012/",
    "https://m.facebook.com/groups/123456/posts/789012",
    "https://web.facebook.com/groups/123456/posts/789012",
    "https://mbasic.facebook.com/groups/123456/posts/789012",
    "https://facebook.com/groups/123456/posts/789012",
    "http://www.facebook.com/groups/123456/posts/789012",
    "https://www.facebook.com/groups/123456/posts/789012?fbclid=ABC123",
    "https://www.facebook.com/groups/123456/posts/789012/?__cft__[0]=xyz",
    "https://www.facebook.com/groups/123456/posts/789012/?__tn__=R&ref=share",
    "https://www.facebook.com/groups/123456/posts/789012?mibextid=abc",
    "HTTPS://WWW.FACEBOOK.COM/groups/123456/posts/789012",
]


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", URL_VARIANTS)
def test_all_variants_normalize_to_one_canonical_url(url):
    assert normalize_facebook_url(url) == CANONICAL


def test_variants_collapse_to_a_single_value():
    assert len({normalize_facebook_url(u) for u in URL_VARIANTS}) == 1


def test_different_posts_do_not_collide():
    other = "https://www.facebook.com/groups/123456/posts/999999"

    assert normalize_facebook_url(other) != CANONICAL


def test_identity_query_parameters_are_preserved():
    normalized = normalize_facebook_url(
        "https://www.facebook.com/permalink.php"
        "?story_fbid=999&id=111&fbclid=drop_me"
    )

    assert "story_fbid=999" in normalized
    assert "id=111" in normalized
    assert "fbclid" not in normalized


def test_empty_url_normalizes_to_empty_string():
    assert normalize_facebook_url("") == ""
    assert normalize_facebook_url(None) == ""


def test_non_facebook_url_is_still_normalized():
    normalized = normalize_facebook_url(
        "https://example.com/page/?utm_source=fb"
    )

    assert normalized == "https://example.com/page"


# ---------------------------------------------------------------------------
# Post ID extraction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.facebook.com/groups/123/posts/456", "456"),
        ("https://www.facebook.com/groups/123/permalink/456", "456"),
        ("https://www.facebook.com/someuser/posts/456", "456"),
        ("https://www.facebook.com/permalink.php?story_fbid=456&id=1", "456"),
        ("https://www.facebook.com/reel/456", "456"),
        ("https://www.facebook.com/videos/456", "456"),
    ],
)
def test_extract_post_id_from_url_shapes(url, expected):
    assert extract_post_id(url) == expected


def test_legacy_id_wins_over_url_parsing():
    assert extract_post_id(CANONICAL, legacy_id="legacy-1") == "legacy-1"


def test_extract_post_id_handles_missing_input():
    assert extract_post_id("") == ""
    assert extract_post_id(None) == ""


# ---------------------------------------------------------------------------
# Text fingerprinting
# ---------------------------------------------------------------------------

def test_fingerprint_ignores_case_punctuation_and_whitespace():
    a = "We need a NEW website for our bakery -- any recommendations?"
    b = "we need a new website for our bakery   any recommendations"

    assert text_fingerprint(a) == text_fingerprint(b)
    assert text_fingerprint(a) != ""


def test_fingerprint_differs_for_different_text():
    a = "We need a new website for our bakery, any recommendations please?"
    b = "We need managed IT support for our dental clinic in Toronto now."

    assert text_fingerprint(a) != text_fingerprint(b)


def test_short_text_has_no_fingerprint():
    assert text_fingerprint("too short") == ""


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def make_post(url, legacy_id=None, text=None, author="Alice Smith"):
    return {
        "url": url,
        "legacyId": legacy_id,
        "text": text or (
            "We need a new website for our Toronto bakery with online "
            "ordering, can anyone recommend a developer?"
        ),
        "user": {"name": author, "id": "u1"},
    }


def test_duplicate_detected_across_url_variants():
    posts = [
        make_post(URL_VARIANTS[0]),
        make_post(URL_VARIANTS[2]),
        make_post(URL_VARIANTS[8]),
    ]

    result = deduplicate_posts(posts)

    assert len(result.unique) == 1
    assert result.duplicate_count == 2


def test_duplicate_detected_by_legacy_id_even_with_different_urls():
    posts = [
        make_post("https://www.facebook.com/groups/1/posts/111", "same-id"),
        make_post("https://www.facebook.com/groups/2/posts/222", "same-id"),
    ]

    result = deduplicate_posts(posts)

    assert len(result.unique) == 1
    assert result.duplicate_count == 1


def test_distinct_posts_are_both_kept():
    posts = [
        make_post(
            "https://www.facebook.com/groups/1/posts/111",
            "id-1",
            text="We need a new website for our Toronto bakery please help",
        ),
        make_post(
            "https://www.facebook.com/groups/1/posts/222",
            "id-2",
            text="Looking for managed IT support for a dental clinic here",
            author="Bob Jones",
        ),
    ]

    result = deduplicate_posts(posts)

    assert len(result.unique) == 2
    assert result.duplicate_count == 0


def test_same_text_from_different_authors_is_not_merged():
    body = (
        "Looking for a developer to build an online booking system for us "
        "as soon as possible, any recommendations?"
    )
    posts = [
        make_post(
            "https://www.facebook.com/groups/1/posts/111",
            "id-1",
            text=body,
            author="Alice",
        ),
        make_post(
            "https://www.facebook.com/groups/1/posts/222",
            "id-2",
            text=body,
            author="Bob",
        ),
    ]

    result = deduplicate_posts(posts)

    assert len(result.unique) == 2


def test_existing_airtable_records_block_reimport():
    records = [
        {"fields": {"Url": URL_VARIANTS[0], "Text": "x", "User name": "A"}}
    ]
    seen = build_seen_keys(
        records,
        url_field="Url",
        text_field="Text",
        author_field="User name",
    )

    result = deduplicate_posts([make_post(URL_VARIANTS[3])], seen)

    assert len(result.unique) == 0
    assert result.duplicate_count == 1


def test_is_duplicate_matches_on_any_key():
    identity = build_identity(
        url=CANONICAL,
        legacy_id="abc",
        text="A sufficiently long body of text for fingerprinting here.",
        author="Alice",
    )

    assert is_duplicate(identity, {f"url:{CANONICAL}"}) is True
    assert is_duplicate(identity, {"id:abc"}) is True
    assert is_duplicate(identity, {"id:nope"}) is False


def test_post_with_no_identity_is_not_imported():
    result = deduplicate_posts([{"url": "", "text": "", "user": {}}])

    assert len(result.unique) == 0
    assert result.duplicate_count == 1
