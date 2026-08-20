"""Tests for notification ordering, deduplication, and county limits."""

import json
import logging
import sqlite3
from unittest import mock

import pytest

from rent591_notifier.database import (
    delivery_status,
    init_db,
    insert_notified_listing,
    listing_exists,
)
from rent591_notifier.notifier import (
    MAX_RESULTS_PER_REGION,
    PAGES_PER_REGION,
    RESULTS_PER_PAGE,
    crawl_and_notify,
)


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


def listing_with_title(listing_id, title, **fields):
    item = listing(listing_id)
    item["title"] = title
    item.update(fields)
    return item


@pytest.mark.asyncio
async def test_page_logs_explain_short_results_and_cross_page_duplicates(
    tmp_path, caplog
):
    config_path, _ = write_config(tmp_path, "  - region: 新北市\n")
    responses = [
        payload("新北市", [listing("one"), listing("two")]),
        payload("新北市", [listing("two")]),
        payload("新北市", []),
        payload("新北市", []),
        payload("新北市", []),
    ]

    async def notify(_, __):
        return None

    caplog.set_level(logging.INFO, logger="rent591_notifier.notifier")
    with mock.patch("rent591_notifier.notifier.crawl_rent_list", side_effect=responses):
        await crawl_and_notify(config_path, notify, run_in_thread=False)

    messages = "\n".join(caplog.messages)
    assert "page=1 source=2 accepted=2 duplicates=0" in messages
    assert "page=2 source=1 accepted=0 duplicates=1" in messages
    assert (
        "unique_listings=2 short_pages=5 cross_page_duplicates=1 "
        "invalid_listings=0 parser_skipped=0"
    ) in messages


@pytest.mark.asyncio
async def test_delivery_failure_logs_retry_context(tmp_path, caplog):
    config_path, _ = write_config(tmp_path, "  - region: 新北市\n")

    async def notify(_, __):
        raise RuntimeError("Telegram unavailable")

    caplog.set_level(logging.WARNING, logger="rent591_notifier.notifier")
    with mock.patch(
        "rent591_notifier.notifier.crawl_rent_list",
        return_value=payload("新北市", [listing("failed")]),
    ):
        await crawl_and_notify(config_path, notify, run_in_thread=False)

    messages = "\n".join(caplog.messages)
    assert "Notification outcome is uncertain" in messages
    assert "region=新北市 listing_id=failed kind=new attempt=1" in messages
    assert "Delivery marked ambiguous" in messages


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
    with mock.patch("rent591_notifier.notifier.crawl_rent_list", return_value=response):
        summary = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert notifications == ["new", "failed"]
    assert summary == {
        "fetched": 3,
        "notified": 1,
        "skipped": 1,
        "failed": 1,
        "ambiguous": 1,
        "retried": 0,
        "parse_failed": 0,
        "filtered": 0,
        "regions": [
            {
                "region": "新北市",
                "crawled": 3,
                "processed": 3,
                "retried": 0,
                "matched": 1,
                "pushed": 1,
                "failed": 1,
                "filtered": 0,
            }
        ],
    }
    assert summary["regions"][0]["processed"] == sum(
        summary["regions"][0][key]
        for key in ("matched", "pushed", "failed", "filtered")
    )
    conn = init_db(db_path)
    assert listing_exists(conn, 3, "new")
    assert not listing_exists(conn, 3, "failed")
    assert delivery_status(conn, 3, "failed") == "ambiguous"
    conn.close()

    retried = []

    async def retry(_, item):
        retried.append(item["id"])

    with mock.patch("rent591_notifier.notifier.crawl_rent_list", return_value=response):
        second = await crawl_and_notify(config_path, retry, run_in_thread=False)
    assert retried == ["failed"]
    assert second["notified"] == 1
    assert second["skipped"] == 2
    assert second["ambiguous"] == 0
    assert second["regions"] == [
        {
            "region": "新北市",
            "crawled": 3,
            "processed": 3,
            "retried": 1,
            "matched": 2,
            "pushed": 1,
            "failed": 0,
            "filtered": 0,
        }
    ]


@pytest.mark.asyncio
async def test_crawls_five_pages_and_processes_at_most_150_results_per_county(
    tmp_path,
):
    config_path, db_path = write_config(
        tmp_path,
        "  - region: 新北市\n    sections: [土城區, 中和區]\n",
    )
    notified = []

    async def notify(_, item):
        notified.append(item["id"])

    def crawl_page(**kwargs):
        page_number = kwargs["page"]
        items = [
            listing(f"p{page_number}-{index}") for index in range(RESULTS_PER_PAGE + 10)
        ]
        return payload("新北市", items)

    with mock.patch(
        "rent591_notifier.notifier.crawl_rent_list", side_effect=crawl_page
    ) as crawl:
        summary = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert [call.kwargs["page"] for call in crawl.call_args_list] == list(
        range(1, PAGES_PER_REGION + 1)
    )
    assert len(notified) == MAX_RESULTS_PER_REGION == 150
    assert summary["fetched"] == 150
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT count(*) FROM 'listings_新北市'").fetchone()[0]
    region_values = conn.execute(
        "SELECT DISTINCT region FROM 'listings_新北市'"
    ).fetchall()
    conn.close()
    assert count == 150
    assert region_values == [("新北市",)]


@pytest.mark.asyncio
async def test_summary_includes_each_countys_crawled_matched_and_pushed_items(tmp_path):
    config_path, db_path = write_config(
        tmp_path, "  - region: 新北市\n  - region: 台北市\n"
    )
    conn = init_db(db_path, [3, 1])
    insert_notified_listing(conn, listing("matched"), 3, "before")
    conn.close()

    async def notify(_, __):
        return None

    def crawl(**kwargs):
        if kwargs["region"] == 3:
            return payload("新北市", [listing("matched"), listing("new")])
        return payload("台北市", [listing("taipei-new")])

    with mock.patch("rent591_notifier.notifier.crawl_rent_list", side_effect=crawl):
        summary = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert summary["regions"] == [
        {
            "region": "新北市",
            "crawled": 2,
            "processed": 2,
            "retried": 0,
            "matched": 1,
            "pushed": 1,
            "failed": 0,
            "filtered": 0,
        },
        {
            "region": "台北市",
            "crawled": 1,
            "processed": 1,
            "retried": 0,
            "matched": 0,
            "pushed": 1,
            "failed": 0,
            "filtered": 0,
        },
    ]


@pytest.mark.asyncio
async def test_pending_delivery_retries_after_it_drops_out_of_crawled_page(tmp_path):
    config_path, db_path = write_config(tmp_path, "  - region: 新北市\n")
    attempts = []

    async def notify(_, item):
        attempts.append(item["id"])
        if len(attempts) == 1:
            raise RuntimeError("temporary failure")

    responses = [payload("新北市", [listing("gone")])] * PAGES_PER_REGION
    responses += [payload("新北市", [])] * PAGES_PER_REGION
    with mock.patch("rent591_notifier.notifier.crawl_rent_list", side_effect=responses):
        first = await crawl_and_notify(config_path, notify, run_in_thread=False)
        second = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert first["regions"][0]["failed"] == 1
    assert second["regions"] == [
        {
            "region": "新北市",
            "crawled": 0,
            "processed": 1,
            "retried": 1,
            "matched": 0,
            "pushed": 1,
            "failed": 0,
            "filtered": 0,
        }
    ]
    assert attempts == ["gone", "gone"]
    conn = init_db(db_path)
    assert delivery_status(conn, 3, "gone") == "sent"
    conn.close()


@pytest.mark.asyncio
async def test_failure_bookkeeping_error_does_not_abort_remaining_listings(tmp_path):
    config_path, _ = write_config(tmp_path, "  - region: 新北市\n")
    response = payload("新北市", [listing("failed"), listing("next")])
    attempted = []

    async def notify(_, item):
        attempted.append(item["id"])
        if item["id"] == "failed":
            raise RuntimeError("Telegram unavailable")

    with (
        mock.patch("rent591_notifier.notifier.crawl_rent_list", return_value=response),
        mock.patch(
            "rent591_notifier.notifier.mark_delivery_ambiguous",
            side_effect=sqlite3.OperationalError("temporarily locked"),
        ),
    ):
        summary = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert attempted == ["failed", "next"]
    assert summary["regions"][0]["failed"] == 1
    assert summary["regions"][0]["pushed"] == 1


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
    with mock.patch("rent591_notifier.notifier.crawl_rent_list", return_value=response):
        await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert notified == ["same"]


@pytest.mark.asyncio
async def test_send_then_database_failure_is_retried_on_next_crawl(tmp_path):
    config_path, db_path = write_config(tmp_path, "  - region: 新北市\n")
    response = payload("新北市", [listing("uncertain")])
    sent = []

    async def notify(_, item):
        sent.append(item["id"])
        return {"chat_id": 123, "message_id": 456}

    with (
        mock.patch("rent591_notifier.notifier.crawl_rent_list", return_value=response),
        mock.patch(
            "rent591_notifier.notifier.insert_notified_listing",
            side_effect=sqlite3.OperationalError("disk full"),
        ),
    ):
        first = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert first["failed"] == 1
    assert first["ambiguous"] == 1
    assert sent == ["uncertain"]

    with mock.patch("rent591_notifier.notifier.crawl_rent_list", return_value=response):
        second = await crawl_and_notify(config_path, notify, run_in_thread=False)
    assert second["notified"] == 1
    assert second["ambiguous"] == 0
    assert second["regions"][0]["pushed"] == 1
    assert sent == ["uncertain", "uncertain"]

    conn = init_db(db_path)
    assert delivery_status(conn, 3, "uncertain") == "sent"
    assert listing_exists(conn, 3, "uncertain")
    conn.close()


@pytest.mark.asyncio
async def test_telegram_receipt_is_persisted(tmp_path):
    config_path, db_path = write_config(tmp_path, "  - region: 新北市\n")
    response = payload("新北市", [listing("receipt")])

    async def notify(_, __):
        return {"chat_id": 123, "message_id": 456}

    with mock.patch("rent591_notifier.notifier.crawl_rent_list", return_value=response):
        await crawl_and_notify(config_path, notify, run_in_thread=False)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT notification_status, telegram_chat_id, telegram_message_id "
        "FROM 'listings_新北市' WHERE id = 'receipt'"
    ).fetchone()
    conn.close()
    assert row == ("sent", 123, 456)


@pytest.mark.asyncio
async def test_ai_filtered_listing_is_recorded_without_notification(tmp_path):
    config_path, db_path = write_config(tmp_path, "  - region: 新北市\n")
    response = payload("新北市", [listing("filtered")])
    evaluated = []
    notifications = []

    async def evaluate(_, item, __):
        evaluated.append(item["id"])
        item["ai"] = {"good": False, "score": 2, "reason": "屋況不佳"}
        return False

    async def notify(_, item):
        notifications.append(item["id"])

    with mock.patch("rent591_notifier.notifier.crawl_rent_list", return_value=response):
        first = await crawl_and_notify(
            config_path, notify, run_in_thread=False, evaluate=evaluate
        )
        second = await crawl_and_notify(
            config_path, notify, run_in_thread=False, evaluate=evaluate
        )

    assert notifications == []
    assert evaluated == ["filtered", "filtered"]
    assert first["filtered"] == 1
    assert first["regions"] == [
        {
            "region": "新北市",
            "crawled": 1,
            "processed": 1,
            "retried": 0,
            "matched": 0,
            "pushed": 0,
            "failed": 0,
            "filtered": 1,
        }
    ]
    assert second["filtered"] == 1
    assert second["skipped"] == 0
    conn = init_db(db_path)
    assert not listing_exists(conn, 3, "filtered")
    status, raw = conn.execute(
        "SELECT notification_status, raw FROM 'listings_新北市' "
        "WHERE id = 'filtered'"
    ).fetchone()
    conn.close()
    assert status == "filtered"
    assert json.loads(raw)["ai"]["good"] is False


@pytest.mark.asyncio
async def test_ai_evaluation_failure_fails_open(tmp_path):
    config_path, _ = write_config(tmp_path, "  - region: 新北市\n")
    response = payload("新北市", [listing("fallback")])
    notifications = []

    async def evaluate(_, __, ___):
        raise RuntimeError("AI unavailable")

    async def notify(_, item):
        notifications.append(item["id"])

    with mock.patch("rent591_notifier.notifier.crawl_rent_list", return_value=response):
        summary = await crawl_and_notify(
            config_path, notify, run_in_thread=False, evaluate=evaluate
        )

    assert notifications == ["fallback"]
    assert summary["notified"] == 1
    assert summary["filtered"] == 0


@pytest.mark.asyncio
async def test_keyword_filtered_listing_is_rechecked_and_sent_after_filter_removal(
    tmp_path,
):
    config_path, db_path = write_config(
        tmp_path,
        "  - region: 新北市\n    exclude_keywords: [頂樓加蓋, PET]\n",
    )
    response = payload(
        "新北市",
        [
            listing_with_title("blocked", "含有頂樓加蓋的房子"),
            listing_with_title("allowed", "Sunny apartment", tags=["可養寵物"]),
        ],
    )
    evaluated = []
    notifications = []

    async def evaluate(_, item, __):
        evaluated.append(item["id"])
        return True

    async def notify(_, item):
        notifications.append(item["id"])

    with mock.patch("rent591_notifier.notifier.crawl_rent_list", return_value=response):
        first = await crawl_and_notify(
            config_path, notify, run_in_thread=False, evaluate=evaluate
        )
        second = await crawl_and_notify(
            config_path, notify, run_in_thread=False, evaluate=evaluate
        )
        config_path.write_text(
            "database: listings.db\ncrawl:\n  - region: 新北市\n",
            encoding="utf-8",
        )
        third = await crawl_and_notify(
            config_path, notify, run_in_thread=False, evaluate=evaluate
        )

    assert notifications == ["allowed", "blocked"]
    assert evaluated == ["allowed", "blocked"]
    assert first["filtered"] == 1
    assert first["notified"] == 1
    assert second["filtered"] == 1
    assert second["skipped"] == 1
    assert third["filtered"] == 0
    assert third["notified"] == 1
    assert third["skipped"] == 1
    conn = init_db(db_path)
    assert listing_exists(conn, 3, "blocked")
    assert listing_exists(conn, 3, "allowed")
    conn.close()


@pytest.mark.asyncio
async def test_keyword_filter_replaces_ambiguous_delivery_with_filtered_state(tmp_path):
    config_path, db_path = write_config(tmp_path, "  - region: 新北市\n")
    response = payload("新北市", [listing_with_title("maybe", "頂樓加蓋")])
    attempts = []

    async def notify(_, item):
        attempts.append(item["id"])
        if len(attempts) == 1:
            raise RuntimeError("uncertain")

    with mock.patch("rent591_notifier.notifier.crawl_rent_list", return_value=response):
        first = await crawl_and_notify(config_path, notify, run_in_thread=False)
        config_path.write_text(
            "database: listings.db\ncrawl:\n"
            "  - region: 新北市\n    exclude_keywords: [頂樓加蓋]\n",
            encoding="utf-8",
        )
        second = await crawl_and_notify(config_path, notify, run_in_thread=False)
        conn = init_db(db_path)
        assert delivery_status(conn, 3, "maybe") is None
        assert conn.execute(
            "SELECT notification_status FROM 'listings_新北市' WHERE id = 'maybe'"
        ).fetchone() == ("filtered",)
        conn.close()
        config_path.write_text(
            "database: listings.db\ncrawl:\n  - region: 新北市\n",
            encoding="utf-8",
        )
        third = await crawl_and_notify(config_path, notify, run_in_thread=False)

    assert first["ambiguous"] == 1
    assert second["filtered"] == 1
    assert second["notified"] == 0
    assert third["notified"] == 1
    assert attempts == ["maybe", "maybe"]
    conn = init_db(db_path)
    assert delivery_status(conn, 3, "maybe") == "sent"
    assert listing_exists(conn, 3, "maybe")
    conn.close()
