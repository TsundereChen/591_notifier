"""Notification-safe crawl orchestration."""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime

from crawler import crawl_rent_list
from main import init_db, insert_notified_listing, listing_exists, load_config

MAX_RESULTS_PER_REGION = 30
LOGGER = logging.getLogger(__name__)


async def crawl_and_notify(config_path, notify, run_in_thread=True):
    """Notify unseen listings, storing each only after notification succeeds.

    `notify` is an async callable receiving `(region_name, listing)`. A failed
    notification is deliberately not persisted, so a later crawl can retry it.
    """
    config = load_config(config_path)
    jobs_by_region = defaultdict(list)
    for job in config["jobs"]:
        jobs_by_region[job["region_id"]].append(job)

    conn = init_db(config["database"], regions=jobs_by_region)
    summary = {"fetched": 0, "notified": 0, "skipped": 0, "failed": 0}
    now = datetime.now().isoformat(timespec="seconds")
    try:
        for region_id, jobs in jobs_by_region.items():
            considered = set()
            for job in jobs:
                if len(considered) >= MAX_RESULTS_PER_REGION:
                    break
                kwargs = {
                    "region": region_id,
                    "sections": job["section_ids"] or None,
                    "kinds": job["kind_ids"] or None,
                    "price_min": job["price_min"],
                    "price_max": job["price_max"],
                    "page": 1,
                }
                if run_in_thread:
                    payload = await asyncio.to_thread(crawl_rent_list, **kwargs)
                else:
                    payload = crawl_rent_list(**kwargs)
                data = json.loads(payload)
                for listing in data["listings"]:
                    listing_id = str(listing["id"])
                    if listing_id in considered:
                        continue
                    considered.add(listing_id)
                    summary["fetched"] += 1
                    if listing_exists(conn, region_id, listing_id):
                        summary["skipped"] += 1
                        continue
                    try:
                        await notify(data["region"], listing)
                    except Exception:
                        LOGGER.exception("Failed to notify listing %s", listing_id)
                        summary["failed"] += 1
                        continue
                    insert_notified_listing(conn, listing, region_id, now)
                    summary["notified"] += 1
                    if len(considered) >= MAX_RESULTS_PER_REGION:
                        break
    finally:
        conn.close()
    return summary
