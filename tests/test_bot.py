"""Pure unit tests for bot menus, cron parsing, and message rendering."""

import pytest

from bot import (
    _config_summary,
    _cron_trigger,
    _listing_message,
    _regions_view,
)


def test_valid_cron_uses_configured_timezone():
    trigger = _cron_trigger({"schedule": "*/10 * * * *", "timezone": "Asia/Taipei"})
    assert str(trigger.timezone) == "Asia/Taipei"


@pytest.mark.parametrize(
    "data",
    [
        {"schedule": "not cron", "timezone": "Asia/Taipei"},
        {"schedule": "* * * * *", "timezone": "Mars/Olympus"},
    ],
)
def test_invalid_schedule_settings(data):
    with pytest.raises(ValueError):
        _cron_trigger(data)


def test_regions_menu_marks_enabled_region():
    _, keyboard = _regions_view({"crawl": [{"region": "新北市"}]})
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "✅ 新北市" in labels
    assert "▫️ 台北市" in labels
    assert all(len(button.callback_data) <= 64 for row in keyboard.inline_keyboard for button in row)


def test_listing_message_contains_essential_fields():
    text = _listing_message(
        "新北市",
        {
            "title": "Sunny apartment",
            "price": "20,000元/月",
            "location": "土城區-中央路",
            "kind": "整層住家",
            "layout": "2房1廳",
            "area": "20坪",
            "url": "https://rent.591.com.tw/123",
        },
    )
    assert "Sunny apartment" in text
    assert "20,000元/月" in text
    assert "土城區-中央路" in text
    assert text.endswith("https://rent.591.com.tw/123")


def test_config_summary_handles_all_filters():
    text = _config_summary({
        "schedule": "0 * * * *",
        "timezone": "Asia/Taipei",
        "crawl": [{"region": "台北市", "sections": [], "kinds": [], "price": {}}],
    })
    assert "台北市" in text
    assert text.count("全部") == 2
