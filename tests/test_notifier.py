"""Tests for notification ordering, deduplication, and county limits."""

import json
import sqlite3
from unittest import mock

import pytest

from rent591_notifier.database import (
    delivery_status,
    init_db,
    insert_notified_listing,
    listing_exists,
)
from rent591_notifier.notifier import MAX_RESULTS_PER_REGION, crawl_and_notify


def write_config(tmp_path, crawl):
    db_path = tmp_path / "listings.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "database: " + str(db_path) + "\ncrawl:\n" + crawl,
        encoding="utf-8",
    )
    return config_path, db_path


def payload(region, listings):
    return json.dumps({"region": region, "count": len(listings), "listings": listings})


def listing(listing_id):
    return {
        "id": str(listing_id),
        "title": f"Listing {listing_id}",
        "url": f"https://rent.591.com.tw/{listing_id}",
        "location": "土城區-中央路",
        "price": "10,000元/月",
        "price_value": 10000,
    }


@pytest.mark.asyncio
async def test_store_only_after_success_and_quarantine_uncertain_failures(tmp_path):
    config_path, db_path = write_config(tmp_path, "  - region: 新北市\n")
    conn = init_db(db_path, [3])
    insert_notified_listing(conn, listing("old"), 3, "before")
    conn.close()

    notifications = []

    async def notify(_, item):
        notifications.append(item["id"])
        if item["id"] == "failed":
            raise RuntimeError("Telegram unavailable")

    response = payload("新北市", [listing("old"), listing("new"), listing("failed")])
    with mock.patch(
        "rent591_notifier.notifier.crawl_rent_list", return_value=response
    ):
        summary = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert notifications == ["new", "failed"]
    assert summary == {
        "fetched": 3,
        "notified": 1,
        "skipped": 1,
        "failed": 1,
        "ambiguous": 1,
        "parse_failed": 0,
    }
    conn = init_db(db_path)
    assert listing_exists(conn, 3, "new")
    assert not listing_exists(conn, 3, "failed")
    assert delivery_status(conn, 3, "failed") == "ambiguous"
    conn.close()

    retried = []

    async def retry(_, item):
        retried.append(item["id"])

    with mock.patch(
        "rent591_notifier.notifier.crawl_rent_list", return_value=response
    ):
        second = await crawl_and_notify(config_path, retry, run_in_thread=False)
    assert retried == []
    assert second["notified"] == 0
    assert second["skipped"] == 2
    assert second["ambiguous"] == 1


@pytest.mark.asyncio
async def test_maximum_thirty_results_per_county(tmp_path):
    config_path, db_path = write_config(
        tmp_path,
        "  - region: 新北市\n    sections: [土城區, 中和區]\n",
    )
    first = [listing(f"a{i}") for i in range(40)]
    notified = []

    async def notify(_, item):
        notified.append(item["id"])

    with mock.patch(
        "rent591_notifier.notifier.crawl_rent_list",
        return_value=payload("新北市", first),
    ) as crawl:
        summary = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert crawl.call_count == 1
    assert len(notified) == MAX_RESULTS_PER_REGION == 30
    assert summary["fetched"] == 30
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT count(*) FROM 'listings_新北市'").fetchone()[0]
    region_values = conn.execute(
        "SELECT DISTINCT region FROM 'listings_新北市'"
    ).fetchall()
    conn.close()
    assert count == 30
    assert region_values == [("新北市",)]


@pytest.mark.asyncio
async def test_duplicate_result_within_page_is_considered_once(tmp_path):
    config_path, _ = write_config(
        tmp_path,
        "  - region: 新北市\n    sections: [土城區, 中和區]\n",
    )
    response = payload("新北市", [listing("same")])
    notified = []

    async def notify(_, item):
        notified.append(item["id"])

    response = payload("新北市", [listing("same"), listing("same")])
    with mock.patch(
        "rent591_notifier.notifier.crawl_rent_list", return_value=response
    ):
        await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert notified == ["same"]


@pytest.mark.asyncio
async def test_send_then_database_failure_is_never_automatically_resent(tmp_path):
    config_path, db_path = write_config(tmp_path, "  - region: 新北市\n")
    response = payload("新北市", [listing("uncertain")])
    sent = []

    async def notify(_, item):
        sent.append(item["id"])
        return {"chat_id": 123, "message_id": 456}

    with (
        mock.patch(
            "rent591_notifier.notifier.crawl_rent_list", return_value=response
        ),
        mock.patch(
            "rent591_notifier.notifier.insert_notified_listing",
            side_effect=sqlite3.OperationalError("disk full"),
        ),
    ):
        first = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert first["failed"] == 1
    assert first["ambiguous"] == 1
    assert sent == ["uncertain"]

    with mock.patch(
        "rent591_notifier.notifier.crawl_rent_list", return_value=response
    ):
        second = await crawl_and_notify(config_path, notify, run_in_thread=False)
    assert second["ambiguous"] == 1
    assert sent == ["uncertain"]

    conn = init_db(db_path)
    assert delivery_status(conn, 3, "uncertain") == "ambiguous"
    assert not listing_exists(conn, 3, "uncertain")
    conn.close()


@pytest.mark.asyncio
async def test_telegram_receipt_is_persisted(tmp_path):
    config_path, db_path = write_config(tmp_path, "  - region: 新北市\n")
    response = payload("新北市", [listing("receipt")])

    async def notify(_, __):
        return {"chat_id": 123, "message_id": 456}

    with mock.patch(
        "rent591_notifier.notifier.crawl_rent_list", return_value=response
    ):
        await crawl_and_notify(config_path, notify, run_in_thread=False)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT notification_status, telegram_chat_id, telegram_message_id "
        "FROM 'listings_新北市' WHERE id = 'receipt'"
    ).fetchone()
    conn.close()
    assert row == ("sent", 123, 456)
