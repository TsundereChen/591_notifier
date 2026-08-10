"""Tests for Telegram-managed YAML persistence."""

import os

import pytest

from config_store import ConfigStore


def test_store_bootstraps_and_persists_settings(tmp_path):
    path = tmp_path / "data" / "config.yaml"
    store = ConfigStore(path)

    initial = store.load()
    assert initial["schedule"] == "*/15 * * * *"
    assert initial["crawl"] == []

    store.set_owner(123, 456)
    store.toggle_region(3)
    store.toggle_section(3, 39)
    store.toggle_kind(3, 1)
    store.set_price(3, 10000, 30000)
    store.set_schedule("0 * * * *")

    reloaded = ConfigStore(path).load()
    assert reloaded["telegram"] == {"owner_user_id": 123, "chat_id": 456}
    assert reloaded["schedule"] == "0 * * * *"
    assert reloaded["crawl"] == [
        {
            "region": "新北市",
            "sections": ["土城區"],
            "kinds": ["整層住家"],
            "price": {"min": 10000, "max": 30000},
        }
    ]


def test_toggle_and_clear_filters(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    store.toggle_region("台北市")
    store.toggle_section(1, 5)
    store.toggle_section(1, 7)
    store.toggle_section(1, 5)
    store.toggle_kind(1, 2)
    store.clear_sections(1)
    store.clear_kinds(1)

    job = store.load()["crawl"][0]
    assert job["sections"] == []
    assert job["kinds"] == []

    store.toggle_region(1)
    assert store.load()["crawl"] == []


def test_template_is_copied_when_runtime_config_is_missing(tmp_path):
    template = tmp_path / "template.yaml"
    template.write_text(
        "database: custom.db\nschedule: '0 8 * * *'\ncrawl: []\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime" / "config.yaml"

    data = ConfigStore(runtime, template).load()

    assert runtime.exists()
    assert data["database"] == "custom.db"
    assert data["schedule"] == "0 8 * * *"
    assert data["timezone"] == "Asia/Taipei"


def test_old_pages_option_is_removed(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "database: rent.db\ncrawl:\n  - region: 新北市\n    pages: 5\n",
        encoding="utf-8",
    )

    data = ConfigStore(path).load()

    assert "pages" not in data["crawl"][0]
    assert data["crawl"][0]["sections"] == []


@pytest.mark.parametrize(
    "text, message",
    [
        ("[]\n", "root must be"),
        ("crawl: nope\n", "must be a list"),
        ("crawl: [nope]\n", "must be a mapping"),
        ("crawl: [{region: 新北市}, {region: 3}]\n", "duplicate crawl region"),
    ],
)
def test_invalid_existing_config_fails_without_overwriting(tmp_path, text, message):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        ConfigStore(path)

    assert path.read_text(encoding="utf-8") == text


def test_owner_binding_is_pinned(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    store.bind_owner(123, 123)
    store.bind_owner(123, 123)

    with pytest.raises(PermissionError, match="pinned"):
        store.bind_owner(123, 456)
    assert store.load()["telegram"] == {"owner_user_id": 123, "chat_id": 123}


def test_runtime_config_permissions_are_private(tmp_path):
    path = tmp_path / "config.yaml"
    ConfigStore(path)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_instance_lock_rejects_second_process_lock(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    first = store.acquire_instance_lock()
    try:
        with pytest.raises(RuntimeError, match="單一執行個體"):
            store.acquire_instance_lock()
    finally:
        first.close()

    second = store.acquire_instance_lock()
    second.close()
