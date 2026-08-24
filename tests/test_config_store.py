"""Tests for Telegram-managed YAML persistence."""

import os

import pytest

from rent591_notifier.config_store import ConfigStore


def test_store_bootstraps_and_persists_settings(tmp_path):
    path = tmp_path / "data" / "config.yaml"
    store = ConfigStore(path)

    initial = store.load()
    assert initial["schedule"] == "*/15 * * * *"
    assert initial["crawl"] == []
    assert initial["ai"] == {
        "enabled": False,
        "filter": True,
        "api_endpoint": None,
        "api_key": None,
        "models": [],
        "criteria": None,
        "max_images": 6,
    }

    store.set_owner(123, 456)
    store.toggle_region(3)
    store.toggle_section(3, 39)
    store.toggle_kind(3, 1)
    store.set_price(3, 10000, 30000)
    store.set_schedule("0 * * * *")
    store.set_ai_enabled(True)
    store.set_ai_filter(False)
    store.set_ai_api_endpoint("https://provider.example/v1/")
    store.set_ai_models(["mimo-v2-omni", "openai/gpt-4o"])
    store.set_ai_criteria("重視採光與捷運距離")
    store.set_ai_api_key("test-key")

    reloaded = ConfigStore(path).load()
    assert reloaded["telegram"] == {"owner_user_id": 123, "chat_id": 456}
    assert reloaded["schedule"] == "0 * * * *"
    assert reloaded["ai"] == {
        "enabled": True,
        "filter": False,
        "api_endpoint": "https://provider.example/v1",
        "api_key": "test-key",
        "models": ["mimo-v2-omni", "openai/gpt-4o"],
        "criteria": "重視採光與捷運距離",
        "max_images": 6,
    }
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


def test_exclude_keywords_are_normalized_persisted_and_cleared(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    store.toggle_region("新北市")

    store.set_exclude_keywords(3, ["  頂樓加蓋", "PET", "ｐｅｔ"])
    assert store.load()["crawl"][0]["exclude_keywords"] == ["頂樓加蓋", "PET"]

    store.set_exclude_keywords(3, None)
    assert "exclude_keywords" not in store.load()["crawl"][0]


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
    assert data["ai"]["api_endpoint"] is None


@pytest.mark.parametrize(
    ("provider", "endpoint"),
    [
        ("go", "https://opencode.ai/zen/go/v1"),
        ("zen", "https://opencode.ai/zen/v1"),
    ],
)
def test_legacy_ai_provider_is_migrated(tmp_path, provider, endpoint):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"ai:\n  provider: {provider}\n  api_key: saved-key\n  model: kimi-k3\ncrawl: []\n",
        encoding="utf-8",
    )

    ai_config = ConfigStore(path).load()["ai"]

    assert "provider" not in ai_config
    assert ai_config["api_endpoint"] == endpoint
    assert ai_config["api_key"] == "saved-key"
    assert ai_config["models"] == ["kimi-k3"]


def test_model_ids_used_by_compatible_gateways_are_accepted(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    models = ["openai/gpt-4o", "llama3.1:8b"]

    store.set_ai_models(models)

    assert store.load()["ai"]["models"] == models


def test_old_pages_option_is_removed(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "database: rent.db\ncrawl:\n  - region: 新北市\n    pages: 5\n",
        encoding="utf-8",
    )

    data = ConfigStore(path).load()

    assert "pages" not in data["crawl"][0]
    assert data["crawl"][0]["sections"] == []


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


@pytest.mark.parametrize(
    ("ai_config", "message"),
    [
        ("enabled: nope", "ai.enabled"),
        ("filter: nope", "ai.filter"),
        ("model: invalid model", "ai.models"),
        ("models: not-a-list", "ai.models"),
        ("models: [invalid model]", "ai.models"),
        ("models: [same-model, same-model]", "ai.models"),
        ("max_images: 11", "ai.max_images"),
        ("api_key: 123", "ai.api_key"),
        ("api_endpoint: not-a-url", "ai.api_endpoint"),
        ("api_endpoint: https://user:pass@example.com/v1", "ai.api_endpoint"),
        ("api_endpoint: https://example.com/v1?token=x", "ai.api_endpoint"),
        ("api_endpoint: https://example.com/v1#fragment", "ai.api_endpoint"),
    ],
)
def test_invalid_ai_configuration_is_rejected(tmp_path, ai_config, message):
    path = tmp_path / "config.yaml"
    path.write_text(f"ai:\n  {ai_config}\ncrawl: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        ConfigStore(path)


def test_invalid_exclude_keyword_configuration_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "crawl:\n  - region: 新北市\n    exclude_keywords: [123]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exclude_keywords"):
        ConfigStore(path)
