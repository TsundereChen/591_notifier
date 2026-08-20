from rent591_notifier.keyword_filter import (
    listing_search_text,
    matched_keyword,
    matches_keyword,
    normalize_keywords,
)


def test_normalize_keywords_trims_and_deduplicates_case_insensitively():
    assert normalize_keywords(["  Pet ", "ＰＥＴ", "頂樓加蓋"]) == [
        "Pet",
        "頂樓加蓋",
    ]


def test_listing_keyword_matching_covers_nested_list_card_fields():
    listing = {
        "title": "Sunny apartment pet",
        "tags": ["可養寵物"],
        "nearby_transit": {"type": "metro", "text": "距捷運站 5 分鐘"},
    }

    assert "sunny" in listing_search_text(listing)
    assert matched_keyword(listing, ["頂樓", "PET", "捷運站"]) == "PET"
    assert matches_keyword(listing, ["捷運站"])
    assert not matches_keyword(listing, ["車位"])
