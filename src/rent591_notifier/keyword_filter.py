"""Validation and matching helpers for listing exclusion keywords."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import Any

MAX_KEYWORDS = 50
MAX_KEYWORD_CHARS = 100

_SEARCHABLE_FIELDS = (
    "title",
    "tags",
    "kind",
    "layout",
    "area",
    "floor",
    "community",
    "location",
    "nearby_transit",
    "poster",
    "updated",
    "views",
    "price",
    "description",
    "labels",
    "address",
    "details",
    "rental_notes",
    "facilities",
)


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def normalize_keywords(value: Any, field: str = "exclude_keywords") -> list[str]:
    """Validate, trim, and case-insensitively deduplicate keyword values."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"'{field}' must be a list")
    if len(value) > MAX_KEYWORDS:
        raise ValueError(f"'{field}' must contain at most {MAX_KEYWORDS} keywords")

    result = []
    seen = set()
    for index, keyword in enumerate(value):
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError(f"'{field}[{index}]' must be a non-empty string")
        keyword = keyword.strip()
        if len(keyword) > MAX_KEYWORD_CHARS:
            raise ValueError(
                f"'{field}[{index}]' must be at most {MAX_KEYWORD_CHARS} characters"
            )
        folded = _fold(keyword)
        if folded not in seen:
            seen.add(folded)
            result.append(keyword)
    return result


def _text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if value:
            yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _text_values(child)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _text_values(child)


def listing_search_text(listing: dict[str, Any]) -> str:
    """Flatten text exposed by the list card for keyword matching."""
    return _fold(
        " ".join(
            text
            for field in _SEARCHABLE_FIELDS
            for text in _text_values(listing.get(field))
        )
    )


def matched_keyword(listing: dict[str, Any], keywords: Iterable[str]) -> str | None:
    """Return the first configured keyword found in a listing, if any."""
    searchable = listing_search_text(listing)
    for keyword in keywords:
        if keyword and _fold(keyword) in searchable:
            return keyword
    return None


def matches_keyword(listing: dict[str, Any], keywords: Iterable[str]) -> bool:
    return matched_keyword(listing, keywords) is not None
