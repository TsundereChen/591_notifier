"""Validated, process-safe YAML persistence for Telegram-managed settings."""

from __future__ import annotations

import copy
import fcntl
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .ai import (
    DEFAULT_MAX_IMAGES,
    MAX_CRITERIA_CHARS,
    MAX_IMAGES_LIMIT,
)
from .crawler import (
    KINDS,
    REGIONS,
    SECTIONS,
    _resolve_kinds,
    _resolve_region,
    _resolve_sections,
    _validate_price_range,
)
from .keyword_filter import normalize_keywords

LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "database": "listings.db",
    "schedule": "*/15 * * * *",
    "timezone": "Asia/Taipei",
    "telegram": {"owner_user_id": None, "chat_id": None},
    "ai": {
        "enabled": False,
        "filter": True,
        "api_endpoint": None,
        "api_key": None,
        "models": [],
        "criteria": None,
        "max_images": DEFAULT_MAX_IMAGES,
    },
    "crawl": [],
}

MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")


def _normalize_ai_endpoint(api_endpoint: str | None) -> str | None:
    if api_endpoint is None:
        return None
    if not isinstance(api_endpoint, str) or not api_endpoint.strip():
        raise ValueError("api_endpoint must be a non-empty URL or None")
    api_endpoint = api_endpoint.strip().rstrip("/")
    parsed_endpoint = urlparse(api_endpoint)
    if (
        parsed_endpoint.scheme not in {"http", "https"}
        or not parsed_endpoint.netloc
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.params
        or parsed_endpoint.query
        or parsed_endpoint.fragment
        or any(character.isspace() for character in api_endpoint)
    ):
        raise ValueError(
            "api_endpoint must be a valid HTTP(S) URL without credentials, query, or fragment"
        )
    return api_endpoint


def _normalize_ai_models(models: Any) -> list[str]:
    if not isinstance(models, list):
        raise ValueError("models must be a list")
    normalized = []
    for model in models:
        if not isinstance(model, str) or not MODEL_ID_PATTERN.fullmatch(model.strip()):
            raise ValueError("models must contain valid model ids")
        model = model.strip()
        if model in normalized:
            raise ValueError("models must not contain duplicates")
        normalized.append(model)
    return normalized


class InstanceLock:
    """A lifetime advisory lock preventing two containers sharing one volume."""

    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            LOGGER.error("Notifier instance lock is already held path=%s", self.path)
            raise RuntimeError(
                f"另一個 591 notifier 正在使用 {self.path.parent}；僅允許單一執行個體"
            ) from exc
        self._file = lock_file
        LOGGER.info("Notifier instance lock acquired path=%s", self.path)
        return self

    def close(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


class ConfigStore:
    """Load and atomically update the YAML file edited by the bot."""

    def __init__(self, path: str | os.PathLike[str], template_path=None):
        self.path = Path(path).expanduser().resolve()
        self.template_path = (
            Path(template_path).expanduser().resolve() if template_path else None
        )
        self._lock = threading.RLock()
        self._config_lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._ensure_exists()

    @contextmanager
    def _guard(self) -> Iterator[None]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._config_lock_path.open("a+", encoding="utf-8") as lock_file:
                os.chmod(self._config_lock_path, 0o600)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def acquire_instance_lock(self) -> InstanceLock:
        return InstanceLock(self.path.parent / ".591-notifier.lock").acquire()

    def _ensure_exists(self) -> None:
        with self._guard():
            if self.path.exists():
                data = self._read_unlocked()
                normalized = self._with_defaults(data)
                if data != normalized:
                    self._save_unlocked(normalized)
                    LOGGER.info("Normalized existing config path=%s", self.path)
                else:
                    os.chmod(self.path, 0o600)
                return
            if self.template_path and self.template_path.exists():
                data = yaml.safe_load(self.template_path.read_text(encoding="utf-8"))
            else:
                data = copy.deepcopy(DEFAULT_CONFIG)
            self._save_unlocked(self._with_defaults(data))
            LOGGER.info("Created config path=%s", self.path)

    @staticmethod
    def _optional_integer(value: Any, field: str) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"'{field}' must be an integer or null")
        return value

    @classmethod
    def _with_defaults(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("config root must be a YAML mapping")
        result = copy.deepcopy(data)
        for key, value in DEFAULT_CONFIG.items():
            result.setdefault(key, copy.deepcopy(value))

        if not isinstance(result["database"], str) or not result["database"].strip():
            raise ValueError("'database' must be a non-empty path")
        if not isinstance(result["schedule"], str) or not result["schedule"].strip():
            raise ValueError("'schedule' must be a non-empty cron expression")
        if not isinstance(result["timezone"], str) or not result["timezone"].strip():
            raise ValueError("'timezone' must be a non-empty IANA timezone")

        telegram = result.get("telegram")
        if not isinstance(telegram, dict):
            raise ValueError("'telegram' must be a mapping")
        telegram.setdefault("owner_user_id", None)
        telegram.setdefault("chat_id", None)
        telegram["owner_user_id"] = cls._optional_integer(
            telegram["owner_user_id"], "telegram.owner_user_id"
        )
        telegram["chat_id"] = cls._optional_integer(
            telegram["chat_id"], "telegram.chat_id"
        )

        ai = result.get("ai")
        if not isinstance(ai, dict):
            raise ValueError("'ai' must be a mapping")
        # Convert configurations written before provider selection was removed.
        legacy_provider = ai.pop("provider", None)
        if legacy_provider is not None and legacy_provider not in {"go", "zen"}:
            raise ValueError("'ai.provider' is no longer supported")
        if "api_endpoint" not in ai and legacy_provider in {"go", "zen"}:
            ai["api_endpoint"] = (
                "https://opencode.ai/zen/go/v1"
                if legacy_provider == "go"
                else "https://opencode.ai/zen/v1"
            )
        legacy_model = ai.pop("model", None)
        if legacy_model is not None and "models" in ai:
            raise ValueError("'ai.model' and 'ai.models' cannot both be set")
        if "models" not in ai and legacy_model is not None:
            ai["models"] = [legacy_model]
        unknown_ai_keys = set(ai) - {
            "enabled",
            "filter",
            "api_endpoint",
            "api_key",
            "models",
            "criteria",
            "max_images",
        }
        if unknown_ai_keys:
            raise ValueError(f"'ai' has unknown keys: {sorted(unknown_ai_keys)}")
        ai.setdefault("enabled", False)
        ai.setdefault("filter", True)
        ai.setdefault("api_endpoint", None)
        ai.setdefault("api_key", None)
        ai.setdefault("models", [])
        ai.setdefault("criteria", None)
        ai.setdefault("max_images", DEFAULT_MAX_IMAGES)
        if not isinstance(ai["enabled"], bool):
            raise ValueError("'ai.enabled' must be a boolean")
        if not isinstance(ai["filter"], bool):
            raise ValueError("'ai.filter' must be a boolean")
        try:
            ai["api_endpoint"] = _normalize_ai_endpoint(ai["api_endpoint"])
        except ValueError as exc:
            raise ValueError(f"'ai.api_endpoint' is invalid: {exc}") from exc
        try:
            ai["models"] = _normalize_ai_models(ai["models"])
        except ValueError as exc:
            raise ValueError(f"'ai.models' is invalid: {exc}") from exc
        if ai["criteria"] is not None and (
            not isinstance(ai["criteria"], str) or not ai["criteria"].strip()
        ):
            raise ValueError("'ai.criteria' must be a non-empty string or null")
        if ai["criteria"] is not None and len(ai["criteria"]) > MAX_CRITERIA_CHARS:
            raise ValueError(
                f"'ai.criteria' must be at most {MAX_CRITERIA_CHARS} characters"
            )
        if (
            not isinstance(ai["max_images"], int)
            or isinstance(ai["max_images"], bool)
            or not 1 <= ai["max_images"] <= MAX_IMAGES_LIMIT
        ):
            raise ValueError(
                f"'ai.max_images' must be an integer between 1 and {MAX_IMAGES_LIMIT}"
            )
        if ai["api_key"] is not None and (
            not isinstance(ai["api_key"], str) or not ai["api_key"].strip()
        ):
            raise ValueError("'ai.api_key' must be a non-empty string or null")

        crawl = result.get("crawl")
        if not isinstance(crawl, list):
            raise ValueError("'crawl' must be a list")
        seen_regions: set[int] = set()
        for index, job in enumerate(crawl):
            if not isinstance(job, dict) or "region" not in job:
                raise ValueError(f"crawl[{index}] must be a mapping with 'region'")
            region_id = _resolve_region(job["region"])
            if region_id in seen_regions:
                raise ValueError(
                    f"duplicate crawl region {REGIONS[region_id]!r}; combine its filters"
                )
            seen_regions.add(region_id)
            job.pop("pages", None)
            job.setdefault("sections", [])
            job.setdefault("kinds", [])
            job.setdefault("price", {})
            if "exclude_keywords" in job:
                job["exclude_keywords"] = normalize_keywords(
                    job["exclude_keywords"], f"crawl[{index}].exclude_keywords"
                )
            if not isinstance(job["sections"], list):
                raise ValueError(f"crawl[{index}].sections must be a list")
            if not isinstance(job["kinds"], list):
                raise ValueError(f"crawl[{index}].kinds must be a list")
            if not isinstance(job["price"], dict):
                raise ValueError(f"crawl[{index}].price must be a mapping")
            unknown_price_keys = set(job["price"]) - {"min", "max"}
            if unknown_price_keys:
                raise ValueError(
                    f"crawl[{index}].price has unknown keys: {sorted(unknown_price_keys)}"
                )
            _resolve_sections(region_id, job["sections"])
            _resolve_kinds(job["kinds"])
            _validate_price_range(job["price"].get("min"), job["price"].get("max"))
        return result

    def _read_unlocked(self) -> Any:
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))

    def load(self) -> dict[str, Any]:
        with self._guard():
            return self._with_defaults(self._read_unlocked())

    def save(self, data: dict[str, Any]) -> None:
        with self._guard():
            self._save_unlocked(self._with_defaults(data))

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                yaml.safe_dump(data, temporary, allow_unicode=True, sort_keys=False)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def update(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._guard():
            data = self._with_defaults(self._read_unlocked())
            mutator(data)
            data = self._with_defaults(data)
            self._save_unlocked(data)
            LOGGER.info("Configuration updated path=%s", self.path)
            return copy.deepcopy(data)

    @staticmethod
    def find_job(data: dict[str, Any], region: int | str):
        region_id = _resolve_region(region)
        for job in data["crawl"]:
            if _resolve_region(job["region"]) == region_id:
                return job
        return None

    def bind_owner(self, user_id: int, chat_id: int) -> dict[str, Any]:
        """Bind exactly once; changing owner/chat requires editing YAML explicitly."""
        user_id = int(user_id)
        chat_id = int(chat_id)

        def mutate(data: dict[str, Any]) -> None:
            telegram = data["telegram"]
            if telegram["owner_user_id"] not in (None, user_id):
                raise PermissionError("bot already has another owner")
            if telegram["chat_id"] not in (None, chat_id):
                raise PermissionError("notification chat is already pinned")
            telegram["owner_user_id"] = user_id
            telegram["chat_id"] = chat_id

        return self.update(mutate)

    def set_owner(self, user_id: int, chat_id: int) -> dict[str, Any]:
        """Backward-compatible explicit administrative owner update."""

        def mutate(data: dict[str, Any]) -> None:
            data["telegram"]["owner_user_id"] = int(user_id)
            data["telegram"]["chat_id"] = int(chat_id)

        return self.update(mutate)

    def toggle_region(self, region: int | str) -> dict[str, Any]:
        region_id = _resolve_region(region)

        def mutate(data: dict[str, Any]) -> None:
            job = self.find_job(data, region_id)
            if job:
                data["crawl"].remove(job)
            else:
                data["crawl"].append(
                    {
                        "region": REGIONS[region_id],
                        "sections": [],
                        "kinds": [],
                        "price": {},
                    }
                )

        return self.update(mutate)

    def _ensure_job(self, data: dict[str, Any], region_id: int) -> dict[str, Any]:
        job = self.find_job(data, region_id)
        if job is None:
            job = {
                "region": REGIONS[region_id],
                "sections": [],
                "kinds": [],
                "price": {},
            }
            data["crawl"].append(job)
        return job

    def toggle_section(self, region: int | str, section: int | str):
        region_id = _resolve_region(region)
        section_id = int(section)
        if section_id not in SECTIONS[region_id]:
            raise ValueError("section does not belong to region")

        def mutate(data: dict[str, Any]) -> None:
            job = self._ensure_job(data, region_id)
            selected = [
                key
                for key, name in SECTIONS[region_id].items()
                if key in job.get("sections", []) or name in job.get("sections", [])
            ]
            if section_id in selected:
                selected.remove(section_id)
            else:
                selected.append(section_id)
            job["sections"] = [SECTIONS[region_id][key] for key in selected]

        return self.update(mutate)

    def clear_sections(self, region: int | str):
        region_id = _resolve_region(region)
        return self.update(
            lambda data: self._ensure_job(data, region_id).__setitem__("sections", [])
        )

    def toggle_kind(self, region: int | str, kind: int | str):
        region_id = _resolve_region(region)
        kind_id = int(kind)
        if kind_id not in KINDS:
            raise ValueError("unknown listing kind")

        def mutate(data: dict[str, Any]) -> None:
            job = self._ensure_job(data, region_id)
            selected = [
                key
                for key, name in KINDS.items()
                if key in job.get("kinds", []) or name in job.get("kinds", [])
            ]
            if kind_id in selected:
                selected.remove(kind_id)
            else:
                selected.append(kind_id)
            job["kinds"] = [KINDS[key] for key in selected]

        return self.update(mutate)

    def clear_kinds(self, region: int | str):
        region_id = _resolve_region(region)
        return self.update(
            lambda data: self._ensure_job(data, region_id).__setitem__("kinds", [])
        )

    def set_price(
        self,
        region: int | str,
        price_min: int | None = None,
        price_max: int | None = None,
    ):
        region_id = _resolve_region(region)
        price_min, price_max = _validate_price_range(price_min, price_max)

        def mutate(data: dict[str, Any]) -> None:
            job = self._ensure_job(data, region_id)
            price = {}
            if price_min is not None:
                price["min"] = price_min
            if price_max is not None:
                price["max"] = price_max
            job["price"] = price

        return self.update(mutate)

    def set_exclude_keywords(
        self, region: int | str, keywords: list[str] | None
    ) -> dict[str, Any]:
        region_id = _resolve_region(region)
        normalized = normalize_keywords(keywords)

        def mutate(data: dict[str, Any]) -> None:
            job = self._ensure_job(data, region_id)
            if normalized:
                job["exclude_keywords"] = normalized
            else:
                job.pop("exclude_keywords", None)

        return self.update(mutate)

    def set_schedule(self, expression: str):
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("schedule must be a non-empty cron expression")
        return self.update(lambda data: data.__setitem__("schedule", expression))

    def set_ai_enabled(self, enabled: bool):
        return self.update(
            lambda data: data["ai"].__setitem__("enabled", bool(enabled))
        )

    def set_ai_filter(self, enabled: bool):
        return self.update(lambda data: data["ai"].__setitem__("filter", bool(enabled)))

    def set_ai_models(self, models: list[str]):
        models = _normalize_ai_models(models)
        return self.update(lambda data: data["ai"].__setitem__("models", models))

    def set_ai_criteria(self, criteria: str | None):
        if criteria is not None and (
            not isinstance(criteria, str) or not criteria.strip()
        ):
            raise ValueError("criteria must be a non-empty string or None")
        if criteria is not None and len(criteria.strip()) > MAX_CRITERIA_CHARS:
            raise ValueError(
                f"criteria must be at most {MAX_CRITERIA_CHARS} characters"
            )
        return self.update(
            lambda data: data["ai"].__setitem__(
                "criteria", criteria.strip() if criteria else None
            )
        )

    def set_ai_api_endpoint(self, api_endpoint: str | None):
        api_endpoint = _normalize_ai_endpoint(api_endpoint)
        return self.update(
            lambda data: data["ai"].__setitem__("api_endpoint", api_endpoint)
        )

    def set_ai_api_key(self, api_key: str | None):
        if api_key is not None and (
            not isinstance(api_key, str) or not api_key.strip()
        ):
            raise ValueError("api_key must be a non-empty string or None")
        return self.update(
            lambda data: data["ai"].__setitem__(
                "api_key", api_key.strip() if api_key else None
            )
        )
