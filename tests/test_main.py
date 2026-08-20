"""Offline tests for YAML-driven crawls and SQLite persistence."""

import sqlite3

import pytest

from rent591_notifier.database import (
    ambiguous_deliveries,
    delivery_status,
    init_db,
    insert_notified_listing,
    listing_exists,
    load_config,
    mark_delivery_ambiguous,
    record_filtered_listing,
    reserve_delivery,
    resolve_ambiguous_delivery,
    retryable_deliveries,
)


def write_config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadConfig:
    def test_multiple_regions_and_optional_sections(self, tmp_path):
        path = write_config(
            tmp_path,
            """
database: data/rent.db
crawl:
  - region: 新北市
    sections: [土城區, 中和區]
    kinds: [整層住家, 2]
    price: {min: 10000, max: 30000}
    pages: 1
  - region: 1
""",
        )

        cfg = load_config(path)

        assert cfg == {
            "database": str((tmp_path / "data" / "rent.db").resolve()),
            "jobs": [
                {
                    "region_id": 3,
                    "section_ids": [39, 38],
                    "kind_ids": [1, 2],
                    "price_min": 10000,
                    "price_max": 30000,
                },
                {
                    "region_id": 1,
                    "section_ids": [],
                    "kind_ids": [],
                    "price_min": None,
                    "price_max": None,
                },
            ],
        }

    def test_loads_exclude_keywords(self, tmp_path):
        cfg = load_config(
            write_config(
                tmp_path,
                """
database: rent.db
crawl:
  - region: 新北市
    exclude_keywords: [頂樓加蓋, 雅房]
""",
            )
        )

        assert cfg["jobs"][0]["exclude_keywords"] == ["頂樓加蓋", "雅房"]

    @pytest.mark.parametrize(
        "text, message",
        [
            ("database: rent.db\n", "non-empty 'crawl' list"),
            ("crawl: [{}]\n", "must define 'region'"),
            ("crawl: [{region: 新北市, pages: 2}]\n", "fixed at 5"),
            ("crawl: [{region: 新北市, price: [1, 2]}]\n", "must define"),
            ("crawl: [{region: 新北市, price: {min: 2, max: 1}}]\n", "greater"),
            ("database: ''\ncrawl: [{region: 新北市}]\n", "non-empty path"),
            (
                "crawl: [{region: 新北市}, {region: 3}]\n",
                "duplicate crawl region",
            ),
        ],
    )
    def test_invalid_config(self, tmp_path, text, message):
        with pytest.raises(ValueError, match=message):
            load_config(write_config(tmp_path, text))


class TestDatabase:
    def test_init_creates_parent_and_schema(self, tmp_path):
        db_path = tmp_path / "nested" / "rent.db"
        conn = init_db(db_path, regions=["新北市"])
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info('listings_新北市')")
        }
        conn.close()

        assert db_path.exists()
        assert {"id", "region", "section", "first_seen", "last_seen"} <= columns

    def test_init_upgrades_existing_delivery_ledger_in_place(self, tmp_path):
        db_path = tmp_path / "v2.db"
        old = sqlite3.connect(db_path)
        old.execute(
            "CREATE TABLE delivery_attempts ("
            "region_id INTEGER NOT NULL, listing_id TEXT NOT NULL, "
            "status TEXT NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "telegram_chat_id INTEGER, telegram_message_id INTEGER, "
            "PRIMARY KEY (region_id, listing_id))"
        )
        old.execute(
            "INSERT INTO delivery_attempts VALUES "
            "(3, '123', 'ambiguous', 'before', 'before', NULL, NULL)"
        )
        old.commit()
        old.close()

        conn = init_db(db_path, [3])
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(delivery_attempts)")
        }

        assert {"payload", "attempt_count", "last_error"} <= columns
        assert retryable_deliveries(conn, 3)[0] == {
            "region_id": 3,
            "listing_id": "123",
            "status": "ambiguous",
            "listing": None,
            "attempt_count": 0,
            "started_at": "before",
            "updated_at": "before",
        }
        conn.close()

    def test_init_expands_existing_listing_status_constraint(self, tmp_path):
        db_path = tmp_path / "v3.db"
        old = sqlite3.connect(db_path)
        old.execute(
            'CREATE TABLE "listings_新北市" ('
            "id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, "
            "notification_status TEXT NOT NULL DEFAULT 'sent' "
            "CHECK (notification_status IN ('sent', 'unknown')))"
        )
        old.execute(
            'INSERT INTO "listings_新北市" '
            "(id, first_seen, last_seen) VALUES "
            "('123', 'before', 'before'), ('old-filter', 'before', 'before')"
        )
        old.execute(
            "CREATE TABLE delivery_attempts ("
            "region_id INTEGER NOT NULL, listing_id TEXT NOT NULL, status TEXT NOT NULL, "
            "started_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "telegram_chat_id INTEGER, telegram_message_id INTEGER, payload TEXT, "
            "attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT, "
            "PRIMARY KEY (region_id, listing_id))"
        )
        old.execute(
            "INSERT INTO delivery_attempts "
            "(region_id, listing_id, status, started_at, updated_at, payload, "
            "attempt_count) VALUES (3, 'old-filter', 'sent', 'before', 'before', "
            '\'{"id": "old-filter"}\', 0)'
        )
        old.execute("PRAGMA user_version = 3")
        old.commit()
        old.close()

        conn = init_db(db_path, [3])
        assert record_filtered_listing(
            conn, 3, {"id": "filtered", "title": "Filtered"}, "now"
        )
        rows = conn.execute(
            'SELECT id, notification_status FROM "listings_新北市" ORDER BY id'
        ).fetchall()
        conn.close()

        assert rows == [
            ("123", "sent"),
            ("filtered", "filtered"),
            ("old-filter", "filtered"),
        ]

    def test_init_migrates_combined_table_into_region_table(self, tmp_path):
        db_path = tmp_path / "old.db"
        old = sqlite3.connect(db_path)
        old.execute(
            "CREATE TABLE listings (id TEXT PRIMARY KEY, title TEXT, location TEXT, "
            "region TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)"
        )
        old.execute(
            "INSERT INTO listings VALUES "
            "('123', 'A listing', '土城區-中央路', '新北市', 'before', 'before')"
        )
        old.commit()
        old.close()

        conn = init_db(db_path)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info('listings_新北市')")
        }
        section = conn.execute(
            "SELECT section FROM 'listings_新北市' WHERE id = '123'"
        ).fetchone()[0]
        old_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'listings'"
        ).fetchone()

        assert {"section", "notification_status", "notified_at"} <= columns
        assert section == "土城區"
        assert old_table is None
        assert not listing_exists(conn, "新北市", "123")
        assert reserve_delivery(conn, "新北市", "123", "review").status == "reserved"

        listing = {"id": "123", "title": "A listing", "location": "土城區-中央路"}
        assert insert_notified_listing(conn, listing, "新北市", "after")
        assert listing_exists(conn, "新北市", "123")
        conn.close()

    def test_notified_insert_deduplicates_and_refreshes_metadata(self):
        conn = init_db(":memory:")
        listing = {
            "id": "123",
            "title": "First title",
            "location": "土城區-中央路",
            "price_value": 10000,
        }

        assert insert_notified_listing(conn, listing, "新北市", "2026-01-01T00:00:00")
        listing["title"] = "Updated title"
        listing["price_value"] = 12000
        assert not insert_notified_listing(
            conn, listing, "新北市", "2026-01-02T00:00:00"
        )
        assert listing_exists(conn, "新北市", "123")

        row = conn.execute(
            "SELECT title, price_value, region, section, first_seen, last_seen "
            "FROM 'listings_新北市' WHERE id = '123'"
        ).fetchone()
        conn.close()
        assert row == (
            "Updated title",
            12000,
            "新北市",
            "土城區",
            "2026-01-01T00:00:00",
            "2026-01-02T00:00:00",
        )

    def test_filtered_listing_is_not_considered_delivered(self):
        conn = init_db(":memory:", [3])
        listing = {"id": "123", "title": "Filtered"}

        assert record_filtered_listing(conn, 3, listing, "first")
        assert not record_filtered_listing(conn, 3, listing, "second")
        assert not listing_exists(conn, 3, "123")
        assert conn.execute(
            'SELECT notification_status, last_seen FROM "listings_新北市" '
            "WHERE id = '123'"
        ).fetchone() == ("filtered", "second")
        reservation = reserve_delivery(conn, 3, "123", "third")
        assert reservation.status == "reserved"
        assert insert_notified_listing(
            conn, listing, 3, "fourth", attempt_count=reservation.attempt_count
        )
        assert not record_filtered_listing(conn, 3, listing, "stale-filter")
        assert listing_exists(conn, 3, "123")
        assert delivery_status(conn, 3, "123") == "sent"
        conn.close()

    def test_interrupted_delivery_is_reserved_again_for_automatic_retry(self):
        conn = init_db(":memory:", [3])
        first = reserve_delivery(
            conn, 3, "123", "first", listing={"id": "123", "title": "Listing"}
        )
        assert ambiguous_deliveries(conn) == []
        second = reserve_delivery(conn, 3, "123", "second")
        assert (first.status, first.attempt_count) == ("reserved", 1)
        assert (second.status, second.attempt_count) == ("reserved", 2)
        assert delivery_status(conn, 3, "123") == "sending"
        assert retryable_deliveries(conn, 3)[0]["listing"]["title"] == "Listing"
        conn.close()

    def test_owner_can_resolve_ambiguous_as_already_delivered(self):
        conn = init_db(":memory:", [3])
        reservation = reserve_delivery(conn, 3, "123", "first")
        assert mark_delivery_ambiguous(
            conn,
            3,
            "123",
            "second",
            attempt_count=reservation.attempt_count,
            error="timeout",
        )
        assert ambiguous_deliveries(conn)[0]["last_error"] == "timeout"
        assert resolve_ambiguous_delivery(conn, 3, "123", delivered=True, now="third")
        assert not mark_delivery_ambiguous(
            conn,
            3,
            "123",
            "stale",
            attempt_count=reservation.attempt_count,
            error="late failure",
        )
        assert reserve_delivery(conn, 3, "123", "fourth").status == "sent"
        assert ambiguous_deliveries(conn) == []
        conn.close()

    def test_filtered_write_cannot_replace_owner_reconciliation(self):
        conn = init_db(":memory:", [3])
        listing = {"id": "123", "title": "Filtered after reconciliation"}
        reservation = reserve_delivery(conn, 3, "123", "first", listing=listing)
        assert mark_delivery_ambiguous(
            conn,
            3,
            "123",
            "second",
            attempt_count=reservation.attempt_count,
            error="timeout",
        )
        assert resolve_ambiguous_delivery(conn, 3, "123", delivered=True, now="third")

        assert not record_filtered_listing(conn, 3, listing, "stale-filter")
        assert delivery_status(conn, 3, "123") == "sent"
        assert reserve_delivery(conn, 3, "123", "fourth").status == "sent"
        conn.close()

    def test_retry_decision_keeps_ambiguous_attempt_queued(self):
        conn = init_db(":memory:", [3])
        reservation = reserve_delivery(conn, 3, "123", "first")
        mark_delivery_ambiguous(
            conn,
            3,
            "123",
            "second",
            attempt_count=reservation.attempt_count,
            error="timeout",
        )

        assert resolve_ambiguous_delivery(conn, 3, "123", delivered=False, now="third")
        assert delivery_status(conn, 3, "123") == "ambiguous"
        conn.close()

    def test_legacy_unknown_row_is_reserved_for_automatic_retry(self):
        conn = init_db(":memory:", [3])
        conn.execute(
            'INSERT INTO "listings_新北市" '
            "(id, first_seen, last_seen, notification_status) "
            "VALUES ('123', 'old', 'old', 'unknown')"
        )
        conn.commit()

        assert reserve_delivery(conn, 3, "123", "first").status == "reserved"
        conn.close()

    def test_legacy_conflict_preserves_combined_table(self, tmp_path):
        db_path = tmp_path / "conflict.db"
        conn = init_db(db_path, [3])
        insert_notified_listing(
            conn,
            {"id": "123", "title": "target", "location": "土城區-中央路"},
            3,
            "now",
        )
        conn.execute(
            "CREATE TABLE listings (id TEXT PRIMARY KEY, title TEXT, location TEXT, "
            "region TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO listings VALUES "
            "('123', 'different', '土城區-中央路', '新北市', 'old', 'old')"
        )
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="legacy migration conflict"):
            init_db(db_path)

        check = sqlite3.connect(db_path)
        assert check.execute(
            "SELECT title FROM listings WHERE id = '123'"
        ).fetchone() == ("different",)
        check.close()
