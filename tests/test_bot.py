"""Unit tests for bot menus, authorization, and background crawl control."""

import asyncio
import logging
import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import CommandHandler

from rent591_notifier import bot
from rent591_notifier.bot import (
    MAX_IMAGES_PER_LISTING,
    _allowed_user_id,
    _adjust_price_bound,
    _ai_view,
    _authorized,
    _config_summary,
    _cron_trigger,
    _format_schedule,
    _home_view,
    _listing_message,
    _listing_images,
    _price_view,
    _regions_view,
    _send_listing,
    callback,
    enqueue_crawl,
)
from rent591_notifier.config_store import ConfigStore


class FakeJobQueue:
    def __init__(self, jobs=()):
        self.jobs = list(jobs)
        self.scheduled = []

    def get_jobs_by_name(self, name):
        assert name == bot.CRAWL_JOB_NAME
        return self.jobs

    def run_custom(self, callback, **kwargs):
        self.scheduled.append((callback, kwargs))


@pytest.fixture
def bot_harness(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    store.bind_owner(123, 123)
    application = SimpleNamespace(
        bot_data={
            "config_store": store,
            "allowed_user_id": 123,
            "crawl_task": None,
        },
        bot=SimpleNamespace(
            send_media_group=AsyncMock(),
            send_message=AsyncMock(),
            send_photo=AsyncMock(),
            set_my_commands=AsyncMock(),
        ),
        job_queue=FakeJobQueue(),
    )
    message = SimpleNamespace(
        chat_id=123,
        text="",
        reply_text=AsyncMock(),
    )
    query = SimpleNamespace(
        data="",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=message,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123, type="private"),
        effective_message=message,
        callback_query=query,
    )
    context = SimpleNamespace(application=application, user_data={})
    return SimpleNamespace(
        store=store,
        application=application,
        message=message,
        query=query,
        update=update,
        context=context,
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
    assert all(
        len(button.callback_data) <= 64
        for row in keyboard.inline_keyboard
        for button in row
    )


def test_region_and_filter_views_cover_enabled_and_unselected_states():
    disabled_text, disabled_keyboard = bot._region_view({"crawl": []}, 3)
    assert "狀態：未啟用" in disabled_text
    assert disabled_keyboard.inline_keyboard[0][0].text == "啟用"

    data = {
        "crawl": [
            {
                "region": "新北市",
                "sections": ["土城區"],
                "kinds": ["整層住家"],
                "price": {"min": 10000},
            }
        ]
    }
    enabled_text, _ = bot._region_view(data, 3)
    assert "狀態：已啟用" in enabled_text
    assert "行政區：土城區" in enabled_text
    assert "租金：NT$10,000 以上" in enabled_text

    sections_text, sections_keyboard = bot._sections_view(data, 3)
    assert "已選擇 1 個行政區" in sections_text
    assert any(
        button.text == "✅ 土城區"
        for row in sections_keyboard.inline_keyboard
        for button in row
    )
    empty_sections_text, _ = bot._sections_view({"crawl": []}, 3)
    assert "未勾選時代表全部行政區" in empty_sections_text

    kinds_text, kinds_keyboard = bot._kinds_view(data, 3)
    assert "已選擇 1 種類型" in kinds_text
    assert kinds_keyboard.inline_keyboard[0][0].text == "✅ 整層住家"
    empty_kinds_text, _ = bot._kinds_view({"crawl": []}, 3)
    assert "未勾選時代表全部類型" in empty_kinds_text


@pytest.mark.parametrize(
    ("price_min", "price_max", "expected"),
    [
        (None, None, "不限"),
        (None, 20000, "NT$20,000 以下"),
        (10000, None, "NT$10,000 以上"),
        (10000, 20000, "NT$10,000～20,000"),
    ],
)
def test_format_price_variants(price_min, price_max, expected):
    assert bot._format_price(price_min, price_max) == expected


def test_schedule_view_lists_presets_and_navigation():
    text, keyboard = bot._schedule_view(
        {"schedule": "*/15 * * * *", "timezone": "Asia/Taipei"}
    )
    assert "*/15 * * * *" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "schedule_set:5m"
    assert keyboard.inline_keyboard[-1][0].callback_data == "home"


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


def test_listing_message_omits_empty_optional_details():
    text = _listing_message("台北市", {"id": "123"})
    assert "🏢" not in text
    assert "https://" not in text


def test_listing_message_includes_ai_verdict():
    text = _listing_message(
        "新北市",
        {
            "title": "AI evaluated listing",
            "ai": {"good": False, "score": 3, "reason": "租金偏高"},
        },
    )

    assert "🤖 AI 評估：⚠️ 不推薦（3/10）" in text
    assert "💭 租金偏高" in text


def test_ai_view_shows_status_and_controls(monkeypatch):
    monkeypatch.setattr(bot, "api_key_from_env", lambda provider: "configured")

    text, keyboard = _ai_view(
        {
            "ai": {
                "enabled": True,
                "filter": False,
                "provider": "go",
                "model": "kimi-k3",
                "criteria": "重視採光",
            }
        }
    )

    assert "狀態：啟用" in text
    assert "僅在通知中標註評語" in text
    assert "提供者：OpenCode Go" in text
    assert "API 金鑰：已設定" in text
    assert "評估標準：重視採光" in text
    assert {
        button.callback_data for row in keyboard.inline_keyboard for button in row
    } >= {
        "ai_toggle",
        "ai_mode",
        "ai_criteria",
        "ai_model",
    }


def test_ai_view_recognizes_saved_api_key(monkeypatch):
    monkeypatch.setattr(bot, "api_key_from_env", lambda provider: None)

    text, _ = _ai_view(
        {
            "ai": {
                "enabled": True,
                "filter": False,
                "provider": "go",
                "model": "kimi-k3",
                "api_key": "saved-key",
            }
        }
    )

    assert "API 金鑰：已設定" in text


@pytest.mark.asyncio
async def test_listing_images_loads_detail_album_deduplicates_and_caps(monkeypatch):
    images = [f"https://img.591.com.tw/{index}.jpg" for index in range(12)]
    images.insert(2, images[0])
    detail_crawl = MagicMock(
        return_value=bot.json.dumps({"listings": [{"images": images}]})
    )
    monkeypatch.setattr(bot, "crawl_rent_details", detail_crawl)

    result = await _listing_images(
        {
            "id": "123",
            "url": "https://rent.591.com.tw/123",
            "image": "https://img.591.com.tw/thumbnail.jpg",
        }
    )

    assert result == [f"https://img.591.com.tw/{index}.jpg" for index in range(10)]
    assert len(result) == MAX_IMAGES_PER_LISTING
    detail_crawl.assert_called_once_with("https://rent.591.com.tw/123", delay=0)


@pytest.mark.asyncio
async def test_listing_images_falls_back_to_list_thumbnail(monkeypatch):
    monkeypatch.setattr(
        bot,
        "crawl_rent_details",
        MagicMock(return_value=bot.json.dumps({"listings": [{"error": "failed"}]})),
    )

    result = await _listing_images(
        {
            "id": "123",
            "url": "https://rent.591.com.tw/123",
            "image": "https://img.591.com.tw/thumbnail.jpg",
        }
    )

    assert result == ["https://img.591.com.tw/thumbnail.jpg"]


@pytest.mark.asyncio
async def test_listing_images_reuses_ai_loaded_album(monkeypatch):
    detail_crawl = MagicMock()
    monkeypatch.setattr(bot, "crawl_rent_details", detail_crawl)

    result = await _listing_images(
        {"images": ["https://img.591.com.tw/one.jpg", "https://img.591.com.tw/two.jpg"]}
    )

    assert result == [
        "https://img.591.com.tw/one.jpg",
        "https://img.591.com.tw/two.jpg",
    ]
    detail_crawl.assert_not_called()


@pytest.mark.asyncio
async def test_send_listing_uses_photo_or_media_group():
    telegram_bot = SimpleNamespace(
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(message_id=10),
                SimpleNamespace(message_id=11),
            ]
        ),
        send_message=AsyncMock(),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=9)),
    )

    photo = await _send_listing(
        telegram_bot, 123, "listing text", ["https://img.591.com.tw/one.jpg"]
    )
    assert photo.message_id == 9
    telegram_bot.send_photo.assert_awaited_once_with(
        chat_id=123,
        photo="https://img.591.com.tw/one.jpg",
        caption="listing text",
    )

    album = await _send_listing(
        telegram_bot,
        123,
        "listing text",
        ["https://img.591.com.tw/one.jpg", "https://img.591.com.tw/two.jpg"],
    )
    assert album.message_id == 10
    media = telegram_bot.send_media_group.await_args.kwargs["media"]
    assert [item.media for item in media] == [
        "https://img.591.com.tw/one.jpg",
        "https://img.591.com.tw/two.jpg",
    ]
    assert [item.caption for item in media] == ["listing text", None]
    telegram_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_listing_falls_back_to_text_when_telegram_rejects_media():
    telegram_bot = SimpleNamespace(
        send_media_group=AsyncMock(side_effect=bot.BadRequest("bad album")),
        send_photo=AsyncMock(side_effect=bot.BadRequest("bad photo")),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=12)),
    )

    result = await _send_listing(
        telegram_bot,
        123,
        "listing text",
        ["https://img.591.com.tw/one.jpg", "https://img.591.com.tw/two.jpg"],
    )

    assert result.message_id == 12
    telegram_bot.send_media_group.assert_awaited_once()
    telegram_bot.send_photo.assert_awaited_once()
    telegram_bot.send_message.assert_awaited_once_with(
        chat_id=123, text="listing text", disable_web_page_preview=False
    )


def test_config_summary_is_structured_and_human_readable():
    text = _config_summary(
        {
            "schedule": "*/15 * * * *",
            "timezone": "Asia/Taipei",
            "crawl": [
                {
                    "region": "台北市",
                    "sections": ["中正區"],
                    "kinds": ["整層住家"],
                    "price": {"min": 10000, "max": 30000},
                }
            ],
        }
    )
    assert "執行頻率：每 15 分鐘" in text
    assert "時區：Asia/Taipei" in text
    assert "  - 台北市" in text
    assert "    行政區：中正區" in text
    assert "    物件類型：整層住家" in text
    assert "    租金範圍：NT$10,000～30,000" in text
    assert "*/15 * * * *" not in text


def test_home_view_contains_detailed_human_readable_settings():
    text, _ = _home_view(
        {
            "schedule": "*/15 * * * *",
            "timezone": "Asia/Taipei",
            "crawl": [
                {
                    "region": "新北市",
                    "sections": ["土城區", "中和區"],
                    "kinds": [],
                    "price": {"min": 10000, "max": 30000},
                }
            ],
        }
    )

    assert "執行頻率：每 15 分鐘" in text
    assert "行政區：土城區、中和區" in text
    assert "物件類型：全部" in text
    assert "租金範圍：NT$10,000～30,000" in text


def test_config_summary_handles_no_enabled_regions():
    text = _config_summary(
        {"schedule": "0 * * * *", "timezone": "Asia/Taipei", "crawl": []}
    )

    assert "執行頻率：每小時" in text
    assert "已啟用縣市：\n  無" in text


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("0 8 * * *", "每天 08:00"),
        ("*/7 * * * *", "每 7 分鐘"),
        ("30 22 * * *", "每天 22:30"),
        ("99 99 * * *", "自訂排程（99 99 * * *）"),
        ("0 9 * * 1", "自訂排程（0 9 * * 1）"),
    ],
)
def test_format_schedule(expression, expected):
    assert _format_schedule(expression) == expected


def test_price_view_has_direct_controls_for_each_bound():
    _, keyboard = _price_view(
        {"crawl": [{"region": "新北市", "price": {"min": 10000, "max": 30000}}]},
        3,
    )
    rows = keyboard.inline_keyboard
    assert [button.text for button in rows[6]] == ["−5,000", "最低：10,000", "+5,000"]
    assert [button.text for button in rows[7]] == ["−5,000", "最高：30,000", "+5,000"]
    assert rows[6][0].callback_data == "price_adjust:3:min:-1"
    assert rows[7][2].callback_data == "price_adjust:3:max:1"


@pytest.mark.parametrize(
    ("price_min", "price_max", "bound", "direction", "expected"),
    [
        (10000, 30000, "min", -1, (5000, 30000)),
        (10000, 30000, "min", 1, (15000, 30000)),
        (10000, 30000, "max", -1, (10000, 25000)),
        (10000, 30000, "max", 1, (10000, 35000)),
        (30000, 30000, "min", 1, (30000, 30000)),
        (30000, 30000, "max", -1, (30000, 30000)),
        (None, 30000, "min", -1, (None, 30000)),
        (None, 30000, "min", 1, (5000, 30000)),
        (5000, 30000, "min", -1, (None, 30000)),
        (10000, None, "min", 1, (15000, None)),
        (None, 30000, "max", 1, (None, 35000)),
        (10000, None, "max", -1, (10000, 45000)),
        (10000, None, "max", 1, (10000, 55000)),
    ],
)
def test_adjust_price_bound(price_min, price_max, bound, direction, expected):
    assert _adjust_price_bound(price_min, price_max, bound, direction) == expected


def test_adjust_price_bound_rejects_invalid_actions():
    with pytest.raises(ValueError, match="direction"):
        _adjust_price_bound(10000, 30000, "min", 0)
    with pytest.raises(ValueError, match="unknown price bound"):
        _adjust_price_bound(10000, 30000, "middle", 1)


@pytest.mark.asyncio
async def test_price_callbacks_persist_direct_adjustments_and_clears(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    store.bind_owner(123, 123)
    store.toggle_region(3)
    store.set_price(3, 10000, 30000)
    application = SimpleNamespace(
        bot_data={"config_store": store, "allowed_user_id": 123}
    )
    query = SimpleNamespace(
        data="",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123, type="private"),
        callback_query=query,
    )
    context = SimpleNamespace(application=application, user_data={})

    query.data = "price_adjust:3:min:-1"
    await callback(update, context)
    assert store.load()["crawl"][0]["price"] == {"min": 5000, "max": 30000}

    query.data = "price_adjust:3:max:1"
    await callback(update, context)
    assert store.load()["crawl"][0]["price"] == {"min": 5000, "max": 35000}

    query.data = "price_clear:3:min"
    await callback(update, context)
    assert store.load()["crawl"][0]["price"] == {"max": 35000}

    query.data = "price_clear:3:max"
    await callback(update, context)
    assert store.load()["crawl"][0]["price"] == {}
    assert query.answer.await_count == 4
    assert query.edit_message_text.await_count == 4


@pytest.mark.asyncio
async def test_callback_dispatches_all_configuration_actions(bot_harness, monkeypatch):
    harness = bot_harness
    reschedule = MagicMock()
    enqueue = MagicMock(return_value=object())
    resolve_pending = MagicMock(return_value=True)
    pending_view = AsyncMock(return_value=("pending", bot.InlineKeyboardMarkup([])))
    monkeypatch.setattr(bot, "reschedule", reschedule)
    monkeypatch.setattr(bot, "enqueue_crawl", enqueue)
    monkeypatch.setattr(bot, "_resolve_pending", resolve_pending)
    monkeypatch.setattr(bot, "_pending_view", pending_view)

    async def press(action):
        harness.query.data = action
        await callback(harness.update, harness.context)

    for action in ("home", "regions", "region:3", "region_toggle:3"):
        await press(action)
    assert harness.store.load()["crawl"][0]["region"] == "新北市"

    for action in (
        "sections:3",
        "section_toggle:3:39",
        "sections_clear:3",
        "kinds:3",
        "kind_toggle:3:1",
        "kinds_clear:3",
        "price:3",
        "price_set:3:10-20",
    ):
        await press(action)
    job = harness.store.load()["crawl"][0]
    assert job["sections"] == []
    assert job["kinds"] == []
    assert job["price"] == {"min": 10000, "max": 20000}

    await press("price_custom:3")
    assert harness.context.user_data["awaiting"] == ("price", 3)
    harness.message.reply_text.assert_awaited()

    await press("schedule")
    await press("schedule_set:30m")
    assert harness.store.load()["schedule"] == "*/30 * * * *"
    reschedule.assert_called_once_with(harness.application)

    await press("schedule_custom")
    assert harness.context.user_data["awaiting"] == ("schedule", None)

    await press("show")
    await press("pending")
    pending_view.assert_awaited()

    await press("delivery:sent:3:listing-1")
    await press("delivery:retry:3:listing-2")
    assert resolve_pending.call_args_list[0].args[-1] is True
    assert resolve_pending.call_args_list[1].args[-1] is False

    edits_before_invalid = harness.query.edit_message_text.await_count
    await press("delivery:invalid:3:listing-3")
    assert harness.query.edit_message_text.await_count == edits_before_invalid

    await press("run")
    enqueue.assert_called_once_with(harness.application, 123)
    assert "正在背景執行" in harness.message.reply_text.await_args.args[0]

    edits_before_unknown = harness.query.edit_message_text.await_count
    await press("unknown-action")
    assert harness.query.edit_message_text.await_count == edits_before_unknown


@pytest.mark.asyncio
async def test_ai_callbacks_and_text_input_persist_settings(bot_harness):
    harness = bot_harness

    harness.query.data = "ai_toggle"
    await callback(harness.update, harness.context)
    assert harness.store.load()["ai"]["enabled"] is True

    harness.query.data = "ai_mode"
    await callback(harness.update, harness.context)
    assert harness.store.load()["ai"]["filter"] is False

    harness.query.data = "ai_provider"
    await callback(harness.update, harness.context)
    assert harness.store.load()["ai"]["provider"] == "zen"

    harness.query.data = "ai_provider"
    await callback(harness.update, harness.context)
    assert harness.store.load()["ai"]["provider"] == "go"

    harness.query.data = "ai_criteria"
    await callback(harness.update, harness.context)
    assert harness.context.user_data["awaiting"] == ("ai_criteria", None)
    harness.message.text = "重視採光"
    await bot.text_input(harness.update, harness.context)
    assert harness.store.load()["ai"]["criteria"] == "重視採光"
    assert "/ai" in harness.message.reply_text.await_args.args[0]

    harness.query.data = "ai_model"
    await callback(harness.update, harness.context)
    assert harness.context.user_data["awaiting"] == ("ai_model", None)
    harness.message.text = "mimo-v2-omni"
    await bot.text_input(harness.update, harness.context)
    assert harness.store.load()["ai"]["model"] == "mimo-v2-omni"
    assert "/ai" in harness.message.reply_text.await_args.args[0]

    harness.query.data = "ai_api_key"
    await callback(harness.update, harness.context)
    assert harness.context.user_data["awaiting"] == ("ai_api_key", None)
    harness.message.text = "test-zen-key"
    await bot.text_input(harness.update, harness.context)
    assert harness.store.load()["ai"]["api_key"] == "test-zen-key"
    assert "/ai" in harness.message.reply_text.await_args.args[0]

    harness.query.data = "ai_api_key"
    await callback(harness.update, harness.context)
    harness.message.text = "-"
    await bot.text_input(harness.update, harness.context)
    assert harness.store.load()["ai"]["api_key"] is None


@pytest.mark.asyncio
async def test_callback_rejects_unauthorized_update(bot_harness):
    bot_harness.update.effective_user.id = 999

    await callback(bot_harness.update, bot_harness.context)

    bot_harness.query.answer.assert_awaited_once_with(
        "僅允許指定擁有者在與機器人的私聊中操作。", show_alert=True
    )


def test_allowed_user_id_is_required_and_validated(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)
    with pytest.raises(RuntimeError, match="必須設定"):
        _allowed_user_id()
    with pytest.raises(RuntimeError, match="正整數"):
        _allowed_user_id("0")
    assert _allowed_user_id("123") == 123


def test_allowed_user_id_rejects_non_integer():
    with pytest.raises(RuntimeError, match="必須是整數"):
        _allowed_user_id("not-a-number")


@pytest.mark.asyncio
async def test_authorization_rejects_missing_update_and_wrong_existing_owner(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    store.bind_owner(456, 456)
    application = SimpleNamespace(
        bot_data={"config_store": store, "allowed_user_id": 123}
    )
    missing = SimpleNamespace(effective_user=None, effective_chat=None)
    wrong_owner = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123, type="private"),
    )

    assert not await _authorized(missing, application)
    assert not await _authorized(wrong_owner, application)


@pytest.mark.asyncio
async def test_reject_uses_callback_alert_or_message_reply():
    query = SimpleNamespace(answer=AsyncMock())
    callback_update = SimpleNamespace(callback_query=query, effective_message=None)
    await bot._reject(callback_update)
    query.answer.assert_awaited_once_with(
        "僅允許指定擁有者在與機器人的私聊中操作。", show_alert=True
    )

    message = SimpleNamespace(reply_text=AsyncMock())
    message_update = SimpleNamespace(callback_query=None, effective_message=message)
    await bot._reject(message_update)
    message.reply_text.assert_awaited_once()

    await bot._reject(SimpleNamespace(callback_query=None, effective_message=None))


@pytest.mark.asyncio
async def test_edit_handles_unchanged_message_and_reraises_other_errors():
    keyboard = MagicMock()
    unchanged = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=bot.BadRequest("Message is not modified")
        )
    )
    await bot._edit(unchanged, "same", keyboard)

    failed = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=bot.BadRequest("message missing"))
    )
    with pytest.raises(bot.BadRequest, match="message missing"):
        await bot._edit(failed, "new", keyboard)


def test_reschedule_replaces_existing_job(bot_harness):
    old_job = SimpleNamespace(schedule_removal=MagicMock())
    bot_harness.application.job_queue = FakeJobQueue([old_job])

    bot.reschedule(bot_harness.application)

    old_job.schedule_removal.assert_called_once_with()
    scheduled, kwargs = bot_harness.application.job_queue.scheduled[0]
    assert scheduled is bot.scheduled_crawl
    assert kwargs["name"] == bot.CRAWL_JOB_NAME
    assert kwargs["job_kwargs"]["coalesce"] is True


@pytest.mark.asyncio
async def test_authorization_claims_only_pinned_private_chat(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    application = SimpleNamespace(
        bot_data={"config_store": store, "allowed_user_id": 123}
    )
    private = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123, type="private"),
    )
    assert await _authorized(private, application, claim=True)

    other_private = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
    )
    group = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=-100, type="group"),
    )
    stranger = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_chat=SimpleNamespace(id=999, type="private"),
    )
    assert not await _authorized(other_private, application)
    assert not await _authorized(group, application)
    assert not await _authorized(stranger, application)
    assert store.load()["telegram"] == {"owner_user_id": 123, "chat_id": 123}


@pytest.mark.asyncio
async def test_existing_owner_can_complete_missing_private_chat_binding(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "telegram: {owner_user_id: 123, chat_id: null}\ncrawl: []\n",
        encoding="utf-8",
    )
    store = ConfigStore(path)
    application = SimpleNamespace(
        bot_data={"config_store": store, "allowed_user_id": 123}
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123, type="private"),
    )
    assert await _authorized(update, application, claim=True)
    assert store.load()["telegram"]["chat_id"] == 123


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "claim"),
    [
        ("start", True),
        ("menu", False),
        ("ai_menu", False),
        ("crawl_command", False),
        ("pending_command", False),
    ],
)
async def test_command_handlers_reject_unauthorized_updates(
    bot_harness, monkeypatch, handler_name, claim
):
    authorized = AsyncMock(return_value=False)
    reject = AsyncMock()
    monkeypatch.setattr(bot, "_authorized", authorized)
    monkeypatch.setattr(bot, "_reject", reject)

    await getattr(bot, handler_name)(bot_harness.update, bot_harness.context)

    if claim:
        authorized.assert_awaited_once_with(
            bot_harness.update, bot_harness.application, claim=True
        )
    else:
        authorized.assert_awaited_once_with(bot_harness.update, bot_harness.application)
    reject.assert_awaited_once_with(bot_harness.update)


@pytest.mark.asyncio
async def test_command_handlers_reply_for_authorized_user(bot_harness, monkeypatch):
    authorized = AsyncMock(return_value=True)
    pending_view = AsyncMock(return_value=("pending", bot.InlineKeyboardMarkup([])))
    enqueue = MagicMock(side_effect=[object(), None])
    monkeypatch.setattr(bot, "_authorized", authorized)
    monkeypatch.setattr(bot, "_pending_view", pending_view)
    monkeypatch.setattr(bot, "enqueue_crawl", enqueue)

    await bot.start(bot_harness.update, bot_harness.context)
    await bot.menu(bot_harness.update, bot_harness.context)
    await bot.ai_menu(bot_harness.update, bot_harness.context)
    await bot.crawl_command(bot_harness.update, bot_harness.context)
    await bot.crawl_command(bot_harness.update, bot_harness.context)
    await bot.pending_command(bot_harness.update, bot_harness.context)

    replies = [call.args[0] for call in bot_harness.message.reply_text.await_args_list]
    assert any("591 租屋通知機器人" in reply for reply in replies)
    assert any("AI 物件評估" in reply for reply in replies)
    assert "正在背景執行爬蟲……" in replies
    assert "爬蟲正在執行中，請稍候。" in replies
    assert "pending" in replies


@pytest.mark.asyncio
async def test_text_input_rejects_unauthorized_and_unprompted_text(
    bot_harness, monkeypatch
):
    monkeypatch.setattr(bot, "_authorized", AsyncMock(return_value=False))
    reject = AsyncMock()
    monkeypatch.setattr(bot, "_reject", reject)
    await bot.text_input(bot_harness.update, bot_harness.context)
    reject.assert_awaited_once()

    monkeypatch.setattr(bot, "_authorized", AsyncMock(return_value=True))
    await bot.text_input(bot_harness.update, bot_harness.context)
    assert "請使用 /menu" in bot_harness.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_text_input_validates_and_persists_schedule(bot_harness, monkeypatch):
    reschedule = MagicMock()
    monkeypatch.setattr(bot, "reschedule", reschedule)

    bot_harness.context.user_data["awaiting"] = ("schedule", None)
    bot_harness.message.text = "invalid cron"
    await bot.text_input(bot_harness.update, bot_harness.context)
    assert bot_harness.context.user_data["awaiting"] == ("schedule", None)
    assert "cron" in bot_harness.message.reply_text.await_args.args[0]

    bot_harness.context.user_data["awaiting"] = ("schedule", None)
    bot_harness.message.text = "0 9 * * *"
    await bot.text_input(bot_harness.update, bot_harness.context)
    assert bot_harness.store.load()["schedule"] == "0 9 * * *"
    reschedule.assert_called_once_with(bot_harness.application)
    assert "排程已更新" in bot_harness.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_text_input_validates_and_persists_price(bot_harness):
    bot_harness.store.toggle_region(3)

    bot_harness.context.user_data["awaiting"] = ("price", 3)
    bot_harness.message.text = "not a range"
    await bot.text_input(bot_harness.update, bot_harness.context)
    assert bot_harness.context.user_data["awaiting"] == ("price", 3)
    assert "請輸入兩個數值" in bot_harness.message.reply_text.await_args.args[0]

    bot_harness.context.user_data["awaiting"] = ("price", 3)
    bot_harness.message.text = "30000 10000"
    await bot.text_input(bot_harness.update, bot_harness.context)
    assert bot_harness.context.user_data["awaiting"] == ("price", 3)
    assert "租金範圍無效" in bot_harness.message.reply_text.await_args.args[0]

    bot_harness.context.user_data["awaiting"] = ("price", 3)
    bot_harness.message.text = "10000 -"
    await bot.text_input(bot_harness.update, bot_harness.context)
    assert bot_harness.store.load()["crawl"][0]["price"] == {"min": 10000}
    assert "租金範圍已更新" in bot_harness.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_enqueue_crawl_is_atomic_and_does_not_queue_a_second_run(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(application, status_chat_id=None):
        started.set()
        await release.wait()
        return status_chat_id

    monkeypatch.setattr(bot, "_run_crawler", fake_run)

    class FakeApplication:
        def __init__(self):
            self.bot_data = {"crawl_task": None}

        def create_task(self, coroutine, **kwargs):
            return asyncio.create_task(coroutine, **kwargs)

    application = FakeApplication()
    first = enqueue_crawl(application, 123)
    assert first is not None
    assert enqueue_crawl(application, 123) is None
    await started.wait()
    release.set()
    assert await first == 123
    await asyncio.sleep(0)
    assert application.bot_data["crawl_task"] is None


@pytest.mark.asyncio
async def test_pending_view_handles_empty_and_ambiguous_rows(monkeypatch):
    monkeypatch.setattr(bot, "_pending_rows", lambda store: [])
    text, keyboard = await bot._pending_view(MagicMock())
    assert "沒有結果不明" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "home"

    monkeypatch.setattr(
        bot,
        "_pending_rows",
        lambda store: [{"region_id": 3, "region": "新北市", "listing_id": "abc"}],
    )
    text, keyboard = await bot._pending_view(MagicMock())
    assert "新北市 #abc" in text
    assert "下次爬蟲會自動重試" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "delivery:sent:3:abc"
    assert len(keyboard.inline_keyboard[0]) == 1


def test_pending_database_helpers_close_connections(bot_harness, monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr(bot, "init_db", MagicMock(return_value=connection))
    monkeypatch.setattr(
        bot,
        "ambiguous_deliveries",
        MagicMock(return_value=[{"listing_id": "abc"}]),
    )
    monkeypatch.setattr(bot, "resolve_ambiguous_delivery", MagicMock(return_value=True))

    assert bot._pending_rows(bot_harness.store) == [{"listing_id": "abc"}]
    assert bot.ambiguous_deliveries.call_args.kwargs == {"limit": 5}
    assert bot._resolve_pending(bot_harness.store, 3, "abc", True)
    assert bot.resolve_ambiguous_delivery.call_args.kwargs["delivered"] is True
    assert connection.close.call_count == 2


@pytest.mark.asyncio
async def test_run_crawler_handles_missing_binding_and_empty_configuration(
    bot_harness, caplog
):
    data = bot_harness.store.load()
    data["telegram"]["chat_id"] = None
    bot_harness.store.save(data)
    assert await bot._run_crawler(bot_harness.application) is None
    assert "尚未有 Telegram 對話" in caplog.text

    bot_harness.store.bind_owner(123, 123)
    assert await bot._run_crawler(bot_harness.application, 123) is None
    bot_harness.application.bot.send_message.assert_awaited_once_with(
        123, "尚未啟用任何縣市。"
    )


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:Deprecated since version v22.2")
async def test_run_crawler_retries_telegram(bot_harness, monkeypatch):
    bot_harness.store.toggle_region(3)
    summary = {
        "notified": 1,
        "skipped": 2,
        "failed": 0,
        "ambiguous": 0,
        "parse_failed": 0,
        "regions": [{"region": "新北市", "crawled": 3, "matched": 2, "pushed": 1}],
    }

    async def fake_crawl(config_path, notify):
        receipt = await notify(
            "新北市",
            {
                "id": "abc",
                "title": "Listing",
                "price": "10,000元/月",
                "location": "土城區",
            },
        )
        assert receipt == {"chat_id": 123, "message_id": 456}
        return summary

    sleep = AsyncMock()
    monkeypatch.setattr(bot, "crawl_and_notify", fake_crawl)
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    bot_harness.application.bot.send_message.side_effect = [
        bot.RetryAfter(timedelta(0)),
        SimpleNamespace(message_id=456),
        None,
    ]

    result = await bot._run_crawler(bot_harness.application, 123)

    assert result == summary
    sleep.assert_awaited_once_with(0.25)
    assert bot_harness.application.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_scheduled_run_does_not_send_completion_summary(bot_harness, monkeypatch):
    bot_harness.store.toggle_region(3)
    summary = {
        "regions": [{"region": "新北市", "crawled": 2, "matched": 1, "pushed": 1}]
    }

    async def fake_crawl(config_path, notify):
        return summary

    monkeypatch.setattr(bot, "crawl_and_notify", fake_crawl)

    assert await bot._run_crawler(bot_harness.application) == summary
    bot_harness.application.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_crawler_wires_ai_evaluation_and_filtering(bot_harness, monkeypatch):
    bot_harness.store.toggle_region(3)
    bot_harness.store.set_ai_enabled(True)
    judge = MagicMock(
        return_value=(
            {"good": False, "score": 2, "reason": "屋況不佳"},
            ["https://img.591.com.tw/one.jpg"],
        )
    )

    async def fake_crawl(config_path, notify, *, evaluate):
        listing = {"id": "abc", "url": "https://rent.591.com.tw/abc"}
        assert await evaluate("新北市", listing) is False
        assert listing["ai"]["reason"] == "屋況不佳"
        assert listing["images"] == ["https://img.591.com.tw/one.jpg"]
        return {"regions": []}

    monkeypatch.setattr(bot, "api_key_from_env", lambda provider: "test-key")
    monkeypatch.setattr(bot, "evaluate_listing", judge)
    monkeypatch.setattr(bot, "crawl_and_notify", fake_crawl)

    assert await bot._run_crawler(bot_harness.application) == {"regions": []}
    assert judge.call_args.kwargs["api_key"] == "test-key"


@pytest.mark.asyncio
async def test_run_crawler_logs_ai_evaluation_failure_with_context(
    bot_harness, monkeypatch, caplog
):
    bot_harness.store.toggle_region(3)
    bot_harness.store.set_ai_enabled(True)
    judge = MagicMock(side_effect=RuntimeError("AI provider unavailable"))

    async def fake_crawl(config_path, notify, *, evaluate):
        assert await evaluate("新北市", {"id": "abc"}) is True
        return {"regions": []}

    caplog.set_level(logging.ERROR, logger="rent591_notifier.bot")
    monkeypatch.setattr(bot, "api_key_from_env", lambda provider: "test-key")
    monkeypatch.setattr(bot, "evaluate_listing", judge)
    monkeypatch.setattr(bot, "crawl_and_notify", fake_crawl)

    assert await bot._run_crawler(bot_harness.application) == {"regions": []}
    record = caplog.records[-1]
    assert (
        "AI evaluation failed; delivering listing without AI verdict" in record.message
    )
    assert "region=新北市 listing_id=abc provider=go model=kimi-k3" in record.message
    assert record.exc_info is not None


@pytest.mark.asyncio
async def test_run_crawler_uses_api_key_saved_in_ai_settings(bot_harness, monkeypatch):
    bot_harness.store.toggle_region(3)
    bot_harness.store.set_ai_enabled(True)
    bot_harness.store.set_ai_api_key("saved-key")
    judge = MagicMock(return_value=({"good": True, "score": 8, "reason": "推薦"}, []))

    async def fake_crawl(config_path, notify, *, evaluate):
        assert await evaluate(
            "新北市", {"id": "abc", "url": "https://rent.591.com.tw/abc"}
        )
        return {"regions": []}

    monkeypatch.setattr(bot, "api_key_from_env", lambda provider: None)
    monkeypatch.setattr(bot, "evaluate_listing", judge)
    monkeypatch.setattr(bot, "crawl_and_notify", fake_crawl)

    assert await bot._run_crawler(bot_harness.application) == {"regions": []}
    assert judge.call_args.kwargs["api_key"] == "saved-key"


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:Deprecated since version v22.2")
async def test_run_crawler_reports_exception_and_final_retry_failure(
    bot_harness, monkeypatch
):
    bot_harness.store.toggle_region(3)

    async def failing_crawl(config_path, notify):
        await notify("新北市", {"id": "abc"})

    monkeypatch.setattr(bot, "crawl_and_notify", failing_crawl)
    monkeypatch.setattr(bot.asyncio, "sleep", AsyncMock())
    bot_harness.application.bot.send_message.side_effect = [
        bot.RetryAfter(timedelta(0)),
        bot.RetryAfter(timedelta(0)),
        bot.RetryAfter(timedelta(0)),
        None,
    ]

    assert await bot._run_crawler(bot_harness.application, 123) is None
    assert bot_harness.application.bot.send_message.await_args_list[-1].args == (
        123,
        "爬蟲執行失敗，請查看容器日誌。",
    )


@pytest.mark.asyncio
async def test_scheduled_crawl_enqueues_without_status_chat(bot_harness, monkeypatch):
    enqueue = MagicMock()
    monkeypatch.setattr(bot, "enqueue_crawl", enqueue)

    await bot.scheduled_crawl(bot_harness.context)

    enqueue.assert_called_once_with(bot_harness.application)


@pytest.mark.asyncio
async def test_post_init_and_shutdown_manage_bot_lifecycle(bot_harness, monkeypatch):
    reschedule = MagicMock()
    monkeypatch.setattr(bot, "reschedule", reschedule)
    lock = MagicMock()
    bot_harness.application.bot_data["instance_lock"] = lock

    await bot.post_init(bot_harness.application)
    commands = bot_harness.application.bot.set_my_commands.await_args.args[0]
    assert [command.command for command in commands] == [
        "start",
        "menu",
        "ai",
        "crawl",
        "pending",
    ]
    reschedule.assert_called_once_with(bot_harness.application)

    await bot.post_shutdown(bot_harness.application)
    lock.close.assert_called_once_with()
    bot_harness.application.bot_data["instance_lock"] = None
    await bot.post_shutdown(bot_harness.application)


def test_build_application_registers_handlers_and_releases_lock(tmp_path):
    application = bot.build_application(
        "0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        tmp_path / "config.yaml",
        allowed_user_id=123,
    )
    try:
        assert application.bot_data["allowed_user_id"] == 123
        assert application.bot_data["crawl_task"] is None
        assert len(application.handlers[0]) == 7
        ai_handler = next(
            handler
            for handler in application.handlers[0]
            if isinstance(handler, CommandHandler)
            and handler.commands == frozenset({"ai"})
        )
        assert ai_handler.callback is bot.ai_menu
    finally:
        application.bot_data["instance_lock"].close()


def test_build_application_rejects_mismatched_existing_owner(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "telegram: {owner_user_id: 456, chat_id: 456}\ncrawl: []\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="owner_user_id"):
        bot.build_application(
            "0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            path,
            allowed_user_id=123,
        )


def test_build_application_closes_lock_when_telegram_builder_fails(
    tmp_path, monkeypatch
):
    lock = MagicMock()
    monkeypatch.setattr(ConfigStore, "acquire_instance_lock", lambda self: lock)
    builder = MagicMock()
    builder.token.return_value = builder
    builder.post_init.return_value = builder
    builder.post_shutdown.return_value = builder
    builder.build.side_effect = RuntimeError("builder failed")
    monkeypatch.setattr(bot, "ApplicationBuilder", lambda: builder)

    with pytest.raises(RuntimeError, match="builder failed"):
        bot.build_application(
            "0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            tmp_path / "config.yaml",
            allowed_user_id=123,
        )
    lock.close.assert_called_once_with()


def test_main_requires_token(monkeypatch):
    configure_logging = MagicMock()
    monkeypatch.setattr(bot, "_configure_logging", configure_logging)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        bot.main()

    configure_logging.assert_called_once_with(None)


def test_configure_logging_quiets_third_party_http_success_logs(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.delenv("HTTP_LOG_LEVEL", raising=False)

    bot._configure_logging()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_redacting_formatter_hides_token_in_message_and_traceback():
    token = "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    formatter = bot._RedactingFormatter(
        "%(levelname)s %(message)s", sensitive_values=(token,)
    )
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "POST https://api.telegram.org/bot%s/getMe",
        (token,),
        None,
    )

    message = formatter.format(record)

    assert token not in message
    assert message == ("INFO POST https://api.telegram.org/bot[REDACTED]/getMe")

    try:
        raise RuntimeError(f"request failed for token {token}")
    except RuntimeError:
        exception_record = logging.LogRecord(
            "telegram",
            logging.ERROR,
            __file__,
            1,
            "polling failed",
            (),
            sys.exc_info(),
        )

    exception_message = formatter.format(exception_record)

    assert token not in exception_message
    assert "request failed for token [REDACTED]" in exception_message


def test_redacting_formatter_hides_record_specific_secret_in_traceback():
    api_key = "secret-ai-key"
    formatter = bot._RedactingFormatter("%(levelname)s %(message)s")

    try:
        raise RuntimeError(f"AI request failed with {api_key}")
    except RuntimeError:
        record = logging.LogRecord(
            "rent591_notifier.bot",
            logging.ERROR,
            __file__,
            1,
            "AI evaluation failed",
            (),
            sys.exc_info(),
        )
    record.sensitive_values = (api_key,)

    message = formatter.format(record)

    assert api_key not in message
    assert "AI request failed with [REDACTED]" in message


def test_redacting_formatter_hides_unconfigured_telegram_token_shape():
    token = "987654321:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    formatter = bot._RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, "url contains %s", (token,), None
    )

    assert formatter.format(record) == "url contains [REDACTED]"


def test_main_builds_and_runs_polling(monkeypatch):
    application = SimpleNamespace(run_polling=MagicMock())
    build = MagicMock(return_value=application)
    configure_logging = MagicMock()
    monkeypatch.setattr(bot, "build_application", build)
    monkeypatch.setattr(bot, "_configure_logging", configure_logging)
    monkeypatch.setenv(
        "TELEGRAM_BOT_TOKEN",
        "0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )
    monkeypatch.setenv("CONFIG_PATH", "/tmp/test-config.yaml")
    monkeypatch.setenv("CONFIG_TEMPLATE_PATH", "/tmp/template.yaml")

    bot.main()

    configure_logging.assert_called_once_with(
        "0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    build.assert_called_once_with(
        "0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "/tmp/test-config.yaml",
        "/tmp/template.yaml",
    )
    application.run_polling.assert_called_once_with(
        allowed_updates=bot.Update.ALL_TYPES
    )
