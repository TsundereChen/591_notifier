"""Tests for notification ordering, deduplication, and county limits."""

import json
import sqlite3
from unittest import mock

import pytest

from main import init_db, insert_notified_listing, listing_exists
from notifier import MAX_RESULTS_PER_REGION, crawl_and_notify


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
async def test_store_only_after_successful_notification_and_retry_failures(tmp_path):
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
    with mock.patch("notifier.crawl_rent_list", return_value=response):
        summary = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert notifications == ["new", "failed"]
    assert summary == {"fetched": 3, "notified": 1, "skipped": 1, "failed": 1}
    conn = init_db(db_path)
    assert listing_exists(conn, 3, "new")
    assert not listing_exists(conn, 3, "failed")
    conn.close()

    retried = []

    async def retry(_, item):
        retried.append(item["id"])

    with mock.patch("notifier.crawl_rent_list", return_value=response):
        second = await crawl_and_notify(config_path, retry, run_in_thread=False)
    assert retried == ["failed"]
    assert second["notified"] == 1
    assert second["skipped"] == 2


@pytest.mark.asyncio
async def test_maximum_thirty_results_across_jobs_for_same_county(tmp_path):
    config_path, db_path = write_config(
        tmp_path,
        "  - region: 新北市\n    sections: [土城區]\n"
        "  - region: 新北市\n    sections: [中和區]\n",
    )
    first = [listing(f"a{i}") for i in range(20)]
    second = [listing(f"b{i}") for i in range(20)]
    notified = []

    async def notify(_, item):
        notified.append(item["id"])

    with mock.patch(
        "notifier.crawl_rent_list",
        side_effect=[payload("新北市", first), payload("新北市", second)],
    ) as crawl:
        summary = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert crawl.call_count == 2
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
async def test_duplicate_result_across_jobs_is_considered_once(tmp_path):
    config_path, _ = write_config(
        tmp_path,
        "  - region: 新北市\n    sections: [土城區]\n"
        "  - region: 新北市\n    sections: [中和區]\n",
    )
    response = payload("新北市", [listing("same")])
    notified = []

    async def notify(_, item):
        notified.append(item["id"])

    with mock.patch("notifier.crawl_rent_list", side_effect=[response, response]):
        await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert notified == ["same"]
