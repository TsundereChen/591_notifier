"""Notification-safe crawl orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from .crawler import REGIONS, crawl_rent_list
from .database import (
    init_db,
    insert_notified_listing,
    load_config,
    mark_delivery_ambiguous,
    notified_listing_ids,
    reserve_delivery,
)

MAX_RESULTS_PER_REGION = 30
LOGGER = logging.getLogger(__name__)


async def _sync_call(run_in_thread: bool, function, *args, **kwargs):
    if run_in_thread:
        return await asyncio.to_thread(function, *args, **kwargs)
    return function(*args, **kwargs)


def _receipt_ids(receipt: Any) -> tuple[int | None, int | None]:
    if receipt is None:
        return None, None
    if isinstance(receipt, dict):
        return receipt.get("chat_id"), receipt.get("message_id")
    chat = getattr(receipt, "chat", None)
    chat_id = getattr(chat, "id", None) or getattr(receipt, "chat_id", None)
    return chat_id, getattr(receipt, "message_id", None)


async def crawl_and_notify(
    config_path,
    notify: Callable[[str, dict[str, Any]], Awaitable[Any]],
    run_in_thread: bool = True,
) -> dict[str, Any]:
    """Notify at most 30 IDs per county, with durable duplicate suppression.

    A minimal delivery reservation is committed before contacting Telegram. If
    the outcome becomes uncertain, that ID is marked ambiguous and is not sent
    again automatically. Full listing data is written only after Telegram
    accepts the notification.
    """
    config = await _sync_call(run_in_thread, load_config, config_path)
    conn = await _sync_call(
        run_in_thread,
        init_db,
        config["database"],
        [job["region_id"] for job in config["jobs"]],
    )
    summary = {
        "fetched": 0,
        "notified": 0,
        "skipped": 0,
        "failed": 0,
        "ambiguous": 0,
        "parse_failed": 0,
        "regions": [],
    }
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        for job in config["jobs"]:
            region_id = job["region_id"]
            region_summary = {
                "region": REGIONS[region_id],
                "crawled": 0,
                "matched": 0,
                "pushed": 0,
            }
            summary["regions"].append(region_summary)
            kwargs = {
                "region": region_id,
                "sections": job["section_ids"] or None,
                "kinds": job["kind_ids"] or None,
                "price_min": job["price_min"],
                "price_max": job["price_max"],
                "page": 1,
            }
            payload = await _sync_call(run_in_thread, crawl_rent_list, **kwargs)
            data = json.loads(payload)
            if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
                raise ValueError("crawler returned an invalid payload")
            summary["parse_failed"] += int(data.get("parse_error_count", 0))

            page: list[dict[str, Any]] = []
            seen_page: set[str] = set()
            for listing in data["listings"]:
                if not isinstance(listing, dict) or listing.get("id") is None:
                    summary["parse_failed"] += 1
                    continue
                listing_id = str(listing["id"])
                if listing_id in seen_page:
                    continue
                seen_page.add(listing_id)
                page.append(listing)
                if len(page) == MAX_RESULTS_PER_REGION:
                    break

            summary["fetched"] += len(page)
            region_summary["crawled"] = len(page)
            existing = await _sync_call(
                run_in_thread,
                notified_listing_ids,
                conn,
                region_id,
                [str(listing["id"]) for listing in page],
                seen_at=now,
            )
            for listing in page:
                listing_id = str(listing["id"])
                if listing_id in existing:
                    summary["skipped"] += 1
                    region_summary["matched"] += 1
                    continue

                reservation = await _sync_call(
                    run_in_thread, reserve_delivery, conn, region_id, listing_id, now
                )
                if reservation == "sent":
                    summary["skipped"] += 1
                    region_summary["matched"] += 1
                    continue
                if reservation == "ambiguous":
                    summary["ambiguous"] += 1
                    LOGGER.warning(
                        "Skipping listing %s because an earlier send outcome is ambiguous",
                        listing_id,
                    )
                    continue

                try:
                    receipt = await notify(data["region"], listing)
                except Exception:
                    LOGGER.exception(
                        "Notification outcome is uncertain for listing %s", listing_id
                    )
                    await _sync_call(
                        run_in_thread,
                        mark_delivery_ambiguous,
                        conn,
                        region_id,
                        listing_id,
                        now,
                    )
                    summary["failed"] += 1
                    summary["ambiguous"] += 1
                    continue

                chat_id, message_id = _receipt_ids(receipt)
                try:
                    await _sync_call(
                        run_in_thread,
                        insert_notified_listing,
                        conn,
                        listing,
                        region_id,
                        now,
                        telegram_chat_id=chat_id,
                        telegram_message_id=message_id,
                    )
                except Exception:
                    LOGGER.exception(
                        "Telegram accepted listing %s but SQLite finalization failed",
                        listing_id,
                    )
                    try:
                        await _sync_call(
                            run_in_thread,
                            mark_delivery_ambiguous,
                            conn,
                            region_id,
                            listing_id,
                            now,
                        )
                    except Exception:
                        LOGGER.exception(
                            "Could not mark listing %s as ambiguous", listing_id
                        )
                    summary["failed"] += 1
                    summary["ambiguous"] += 1
                    continue
                summary["notified"] += 1
                region_summary["pushed"] += 1
    finally:
        await _sync_call(run_in_thread, conn.close)
    return summary
