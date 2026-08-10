"""Unit tests for bot menus, authorization, and background crawl control."""

import asyncio
from types import SimpleNamespace

import pytest

import bot
from bot import (
    _allowed_user_id,
    _authorized,
    _config_summary,
    _cron_trigger,
    _listing_message,
    _regions_view,
    enqueue_crawl,
)
from config_store import ConfigStore


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
    text = _config_summary(
        {
            "schedule": "0 * * * *",
            "timezone": "Asia/Taipei",
            "crawl": [{"region": "台北市", "sections": [], "kinds": [], "price": {}}],
        }
    )
    assert "台北市" in text
    assert text.count("全部") == 2


def test_allowed_user_id_is_required_and_validated(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)
    with pytest.raises(RuntimeError, match="必須設定"):
        _allowed_user_id()
    with pytest.raises(RuntimeError, match="正整數"):
        _allowed_user_id("0")
    assert _allowed_user_id("123") == 123


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
