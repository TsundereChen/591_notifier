"""Thread-safe YAML persistence for Telegram-managed crawler settings."""

import copy
import os
import threading
from pathlib import Path

import yaml

from crawler import KINDS, REGIONS, SECTIONS, _resolve_region


DEFAULT_CONFIG = {
    "database": "listings.db",
    "schedule": "*/15 * * * *",
    "timezone": "Asia/Taipei",
    "telegram": {"owner_user_id": None, "chat_id": None},
    "crawl": [],
}


class ConfigStore:
    """Load and atomically update the YAML file edited by the bot."""

    def __init__(self, path, template_path=None):
        self.path = Path(path)
        self.template_path = Path(template_path) if template_path else None
        self._lock = threading.RLock()
        self._ensure_exists()

    def _ensure_exists(self):
        with self._lock:
            if self.path.exists():
                data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
                normalized = self._with_defaults(data)
                if data != normalized:
                    self._save_unlocked(normalized)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.template_path and self.template_path.exists():
                data = yaml.safe_load(self.template_path.read_text(encoding="utf-8"))
            else:
                data = copy.deepcopy(DEFAULT_CONFIG)
            self._save_unlocked(self._with_defaults(data))

    @staticmethod
    def _with_defaults(data):
        if not isinstance(data, dict):
            data = {}
        result = copy.deepcopy(data)
        for key, value in DEFAULT_CONFIG.items():
            result.setdefault(key, copy.deepcopy(value))
        if not isinstance(result.get("telegram"), dict):
            result["telegram"] = copy.deepcopy(DEFAULT_CONFIG["telegram"])
        else:
            result["telegram"].setdefault("owner_user_id", None)
            result["telegram"].setdefault("chat_id", None)
        if not isinstance(result.get("crawl"), list):
            result["crawl"] = []
        for job in result["crawl"]:
            if isinstance(job, dict):
                # Older configs supported multiple pages. The bot deliberately
                # fixes this to one page and 30 aggregate results per region.
                job.pop("pages", None)
                job.setdefault("sections", [])
                job.setdefault("kinds", [])
                job.setdefault("price", {})
        return result

    def load(self):
        with self._lock:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            return self._with_defaults(data)

    def save(self, data):
        with self._lock:
            self._save_unlocked(self._with_defaults(data))

    def _save_unlocked(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def update(self, mutator):
        with self._lock:
            data = self.load()
            mutator(data)
            self._save_unlocked(data)
            return copy.deepcopy(data)

    @staticmethod
    def find_job(data, region):
        region_id = _resolve_region(region)
        for job in data["crawl"]:
            if _resolve_region(job["region"]) == region_id:
                return job
        return None

    def set_owner(self, user_id, chat_id):
        def mutate(data):
            data["telegram"]["owner_user_id"] = int(user_id)
            data["telegram"]["chat_id"] = int(chat_id)

        return self.update(mutate)

    def toggle_region(self, region):
        region_id = _resolve_region(region)

        def mutate(data):
            job = self.find_job(data, region_id)
            if job:
                data["crawl"].remove(job)
            else:
                data["crawl"].append({
                    "region": REGIONS[region_id],
                    "sections": [],
                    "kinds": [],
                    "price": {},
                })

        return self.update(mutate)

    def _ensure_job(self, data, region_id):
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

    def toggle_section(self, region, section):
        region_id = _resolve_region(region)
        section_id = int(section)
        if section_id not in SECTIONS[region_id]:
            raise ValueError("section does not belong to region")

        def mutate(data):
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

    def clear_sections(self, region):
        region_id = _resolve_region(region)
        return self.update(
            lambda data: self._ensure_job(data, region_id).__setitem__("sections", [])
        )

    def toggle_kind(self, region, kind):
        region_id = _resolve_region(region)
        kind_id = int(kind)
        if kind_id not in KINDS:
            raise ValueError("unknown listing kind")

        def mutate(data):
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

    def clear_kinds(self, region):
        region_id = _resolve_region(region)
        return self.update(
            lambda data: self._ensure_job(data, region_id).__setitem__("kinds", [])
        )

    def set_price(self, region, price_min=None, price_max=None):
        region_id = _resolve_region(region)

        def mutate(data):
            job = self._ensure_job(data, region_id)
            price = {}
            if price_min is not None:
                price["min"] = int(price_min)
            if price_max is not None:
                price["max"] = int(price_max)
            job["price"] = price

        return self.update(mutate)

    def set_schedule(self, expression):
        return self.update(lambda data: data.__setitem__("schedule", expression))
