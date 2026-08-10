"""Tests for Telegram-managed YAML persistence."""

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
