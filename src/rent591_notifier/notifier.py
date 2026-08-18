"""Notification-safe crawl orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
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
    retryable_deliveries,
)

PAGES_PER_REGION = 5
RESULTS_PER_PAGE = 30
MAX_RESULTS_PER_REGION = PAGES_PER_REGION * RESULTS_PER_PAGE
LOGGER = logging.getLogger(__name__)
SENSITIVE_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])\d{5,16}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _error_description(stage: str, error: Exception) -> str:
    detail = SENSITIVE_TOKEN_PATTERN.sub("[REDACTED]", str(error))
    return f"{stage}: {type(error).__name__}: {detail}"


def _fallback_retry_listing(listing_id: str) -> dict[str, Any]:
    """Build a minimal notification for pre-migration attempts without payloads."""
    listing = {"id": listing_id, "title": f"待重試租屋物件 #{listing_id}"}
    if listing_id.isdigit():
        listing["url"] = f"https://rent.591.com.tw/{listing_id}"
    return listing


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
    """Notify listings from five pages per county with durable deduplication.

    A minimal delivery reservation is committed before contacting Telegram. If
    the outcome becomes uncertain, that ID is marked ambiguous and retried by
    the next crawl. Full listing data is written only after Telegram accepts the
    notification.
    """
    run_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    conn = None
    summary = {
        "fetched": 0,
        "notified": 0,
        "skipped": 0,
        "failed": 0,
        "ambiguous": 0,
        "retried": 0,
        "parse_failed": 0,
        "regions": [],
    }

    async def record_failed_attempt(
        region_id: int,
        listing_id: str,
        attempt_count: int,
        error: Exception,
        stage: str,
    ) -> None:
        description = _error_description(stage, error)
        try:
            updated = await _sync_call(
                run_in_thread,
                mark_delivery_ambiguous,
                conn,
                region_id,
                listing_id,
                _utc_now(),
                attempt_count=attempt_count,
                error=description,
            )
        except Exception:
            LOGGER.exception(
                "Could not record failed delivery run_id=%s region=%s listing_id=%s "
                "attempt=%s stage=%s",
                run_id,
                REGIONS[region_id],
                listing_id,
                attempt_count,
                stage,
            )
            return
        if not updated:
            LOGGER.warning(
                "Failed delivery state was already resolved run_id=%s region=%s "
                "listing_id=%s attempt=%s stage=%s",
                run_id,
                REGIONS[region_id],
                listing_id,
                attempt_count,
                stage,
            )
        else:
            LOGGER.warning(
                "Delivery marked ambiguous run_id=%s region=%s listing_id=%s "
                "attempt=%s stage=%s error=%s",
                run_id,
                REGIONS[region_id],
                listing_id,
                attempt_count,
                stage,
                description,
            )

    try:
        config = await _sync_call(run_in_thread, load_config, config_path)
        LOGGER.info(
            "Crawl started run_id=%s regions=%s database=%s",
            run_id,
            ",".join(REGIONS[job["region_id"]] for job in config["jobs"]),
            config["database"],
        )
        conn = await _sync_call(
            run_in_thread,
            init_db,
            config["database"],
            [job["region_id"] for job in config["jobs"]],
        )
        for job in config["jobs"]:
            region_id = job["region_id"]
            region_name = REGIONS[region_id]
            region_summary = {
                "region": region_name,
                "crawled": 0,
                "processed": 0,
                "retried": 0,
                "matched": 0,
                "pushed": 0,
                "failed": 0,
            }
            summary["regions"].append(region_summary)
            kwargs = {
                "region": region_id,
                "sections": job["section_ids"] or None,
                "kinds": job["kind_ids"] or None,
                "price_min": job["price_min"],
                "price_max": job["price_max"],
            }
            LOGGER.info(
                "Crawl region started run_id=%s region=%s sections=%s kinds=%s "
                "price_min=%s price_max=%s",
                run_id,
                region_name,
                job["section_ids"],
                job["kind_ids"],
                job["price_min"],
                job["price_max"],
            )
            page: list[dict[str, Any]] = []
            seen_page: set[str] = set()
            duplicate_count = 0
            invalid_count = 0
            short_page_count = 0
            page_parse_errors = 0
            for page_number in range(1, PAGES_PER_REGION + 1):
                payload = await _sync_call(
                    run_in_thread, crawl_rent_list, **kwargs, page=page_number
                )
                data = json.loads(payload)
                if not isinstance(data, dict) or not isinstance(
                    data.get("listings"), list
                ):
                    raise ValueError("crawler returned an invalid payload")
                source_count = len(data["listings"])
                parser_errors = int(data.get("parse_error_count", 0))
                summary["parse_failed"] += parser_errors
                page_parse_errors += parser_errors
                if source_count < RESULTS_PER_PAGE:
                    short_page_count += 1
                page_duplicates = 0
                page_invalid = 0
                accepted_count = 0

                for listing in data["listings"][:RESULTS_PER_PAGE]:
                    if not isinstance(listing, dict) or listing.get("id") is None:
                        summary["parse_failed"] += 1
                        page_invalid += 1
                        continue
                    listing_id = str(listing["id"])
                    if listing_id in seen_page:
                        page_duplicates += 1
                        continue
                    seen_page.add(listing_id)
                    page.append(listing)
                    accepted_count += 1
                    if len(page) == MAX_RESULTS_PER_REGION:
                        break

                duplicate_count += page_duplicates
                invalid_count += page_invalid
                LOGGER.info(
                    "Crawl page collected run_id=%s region=%s page=%s source=%s "
                    "accepted=%s duplicates=%s invalid=%s parser_skipped=%s "
                    "ignored_over_page_limit=%s short_page=%s cumulative_unique=%s",
                    run_id,
                    region_name,
                    page_number,
                    source_count,
                    accepted_count,
                    page_duplicates,
                    page_invalid,
                    parser_errors,
                    max(0, source_count - RESULTS_PER_PAGE),
                    source_count < RESULTS_PER_PAGE,
                    len(page),
                )

            summary["fetched"] += len(page)
            region_summary["crawled"] = len(page)
            LOGGER.info(
                "Crawl region fetch complete run_id=%s region=%s unique_listings=%s "
                "short_pages=%s cross_page_duplicates=%s invalid_listings=%s "
                "parser_skipped=%s",
                run_id,
                region_name,
                len(page),
                short_page_count,
                duplicate_count,
                invalid_count,
                page_parse_errors,
            )
            page_by_id = {str(listing["id"]): listing for listing in page}
            pending = await _sync_call(
                run_in_thread, retryable_deliveries, conn, region_id
            )
            pending_ids = {item["listing_id"] for item in pending}
            if pending:
                pending_statuses = ",".join(
                    sorted({item["status"] for item in pending})
                )
                LOGGER.warning(
                    "Retrying incomplete deliveries run_id=%s region=%s count=%s "
                    "statuses=%s",
                    run_id,
                    region_name,
                    len(pending),
                    pending_statuses,
                )
            work_items: list[tuple[dict[str, Any], bool]] = []
            for item in pending:
                listing_id = item["listing_id"]
                listing = page_by_id.get(listing_id) or item.get("listing")
                if not isinstance(listing, dict):
                    listing = _fallback_retry_listing(listing_id)
                else:
                    listing = dict(listing)
                    listing["id"] = listing_id
                work_items.append((listing, True))
            work_items.extend(
                (listing, False)
                for listing in page
                if str(listing["id"]) not in pending_ids
            )
            region_summary["processed"] = len(work_items)
            region_summary["retried"] = len(pending)
            summary["retried"] += len(pending)
            existing = await _sync_call(
                run_in_thread,
                notified_listing_ids,
                conn,
                region_id,
                [str(listing["id"]) for listing, _ in work_items],
                seen_at=_utc_now(),
            )
            for listing, was_pending in work_items:
                listing_id = str(listing["id"])
                if not was_pending and listing_id in existing:
                    summary["skipped"] += 1
                    region_summary["matched"] += 1
                    continue

                delivery_kind = "retry" if was_pending else "new"
                attempt_now = _utc_now()
                reservation = await _sync_call(
                    run_in_thread,
                    reserve_delivery,
                    conn,
                    region_id,
                    listing_id,
                    attempt_now,
                    listing=listing,
                )
                if reservation.status == "sent":
                    summary["skipped"] += 1
                    region_summary["matched"] += 1
                    LOGGER.info(
                        "Delivery already recorded run_id=%s region=%s listing_id=%s "
                        "kind=%s",
                        run_id,
                        region_name,
                        listing_id,
                        delivery_kind,
                    )
                    continue
                if reservation.attempt_count is None:
                    raise RuntimeError("reserved delivery has no attempt count")
                LOGGER.info(
                    "Delivery attempt started run_id=%s region=%s listing_id=%s "
                    "kind=%s attempt=%s",
                    run_id,
                    region_name,
                    listing_id,
                    delivery_kind,
                    reservation.attempt_count,
                )
                try:
                    receipt = await notify(region_name, listing)
                except Exception as exc:
                    LOGGER.exception(
                        "Notification outcome is uncertain run_id=%s region=%s "
                        "listing_id=%s kind=%s attempt=%s",
                        run_id,
                        region_name,
                        listing_id,
                        delivery_kind,
                        reservation.attempt_count,
                    )
                    await record_failed_attempt(
                        region_id,
                        listing_id,
                        reservation.attempt_count,
                        exc,
                        "notification",
                    )
                    summary["failed"] += 1
                    summary["ambiguous"] += 1
                    region_summary["failed"] += 1
                    continue

                chat_id, message_id = _receipt_ids(receipt)
                try:
                    await _sync_call(
                        run_in_thread,
                        insert_notified_listing,
                        conn,
                        listing,
                        region_id,
                        _utc_now(),
                        telegram_chat_id=chat_id,
                        telegram_message_id=message_id,
                        attempt_count=reservation.attempt_count,
                    )
                except Exception as exc:
                    LOGGER.exception(
                        "Telegram accepted listing but SQLite finalization failed "
                        "run_id=%s region=%s listing_id=%s kind=%s attempt=%s",
                        run_id,
                        region_name,
                        listing_id,
                        delivery_kind,
                        reservation.attempt_count,
                    )
                    await record_failed_attempt(
                        region_id,
                        listing_id,
                        reservation.attempt_count,
                        exc,
                        "database finalization",
                    )
                    summary["failed"] += 1
                    summary["ambiguous"] += 1
                    region_summary["failed"] += 1
                    continue
                summary["notified"] += 1
                region_summary["pushed"] += 1
                LOGGER.info(
                    "Delivery succeeded run_id=%s region=%s listing_id=%s kind=%s "
                    "attempt=%s",
                    run_id,
                    region_name,
                    listing_id,
                    delivery_kind,
                    reservation.attempt_count,
                )
            LOGGER.info(
                "Crawl region complete run_id=%s region=%s crawled=%s processed=%s "
                "matched=%s delivered=%s failed=%s retried=%s",
                run_id,
                region_name,
                region_summary["crawled"],
                region_summary["processed"],
                region_summary["matched"],
                region_summary["pushed"],
                region_summary["failed"],
                region_summary["retried"],
            )
    except Exception:
        LOGGER.exception(
            "Crawl failed run_id=%s elapsed_ms=%s",
            run_id,
            round((time.monotonic() - started) * 1000),
        )
        raise
    finally:
        if conn is not None:
            await _sync_call(run_in_thread, conn.close)
    LOGGER.info(
        "Crawl completed run_id=%s elapsed_ms=%s fetched=%s notified=%s "
        "matched=%s failed=%s retried=%s parser_skipped=%s",
        run_id,
        round((time.monotonic() - started) * 1000),
        summary["fetched"],
        summary["notified"],
        summary["skipped"],
        summary["failed"],
        summary["retried"],
        summary["parse_failed"],
    )
    return summary
