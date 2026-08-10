"""Offline tests for YAML-driven crawls and SQLite persistence."""

import sqlite3

import pytest

from main import (
    ambiguous_deliveries,
    delivery_status,
    init_db,
    insert_notified_listing,
    listing_exists,
    load_config,
    reserve_delivery,
    resolve_ambiguous_delivery,
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

    @pytest.mark.parametrize(
        "text, message",
        [
            ("database: rent.db\n", "non-empty 'crawl' list"),
            ("crawl: [{}]\n", "must define 'region'"),
            ("crawl: [{region: 新北市, pages: 2}]\n", "fixed at 1"),
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
        assert reserve_delivery(conn, "新北市", "123", "review") == "ambiguous"
        assert resolve_ambiguous_delivery(
            conn, "新北市", "123", delivered=False, now="retry"
        )
        assert reserve_delivery(conn, "新北市", "123", "after-retry") == "reserved"

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

    def test_interrupted_delivery_becomes_ambiguous_and_is_not_reserved_again(self):
        conn = init_db(":memory:", [3])
        assert reserve_delivery(conn, 3, "123", "first") == "reserved"
        assert reserve_delivery(conn, 3, "123", "second") == "ambiguous"
        assert reserve_delivery(conn, 3, "123", "third") == "ambiguous"
        assert delivery_status(conn, 3, "123") == "ambiguous"
        assert ambiguous_deliveries(conn)[0]["listing_id"] == "123"
        assert resolve_ambiguous_delivery(conn, 3, "123", delivered=False, now="fourth")
        assert reserve_delivery(conn, 3, "123", "fifth") == "reserved"
        conn.close()

    def test_owner_can_resolve_ambiguous_as_already_delivered(self):
        conn = init_db(":memory:", [3])
        assert reserve_delivery(conn, 3, "123", "first") == "reserved"
        assert reserve_delivery(conn, 3, "123", "second") == "ambiguous"
        assert resolve_ambiguous_delivery(conn, 3, "123", delivered=True, now="third")
        assert reserve_delivery(conn, 3, "123", "fourth") == "sent"
        conn.close()

    def test_owner_marks_legacy_unknown_row_as_delivered(self):
        conn = init_db(":memory:", [3])
        conn.execute(
            'INSERT INTO "listings_新北市" '
            "(id, first_seen, last_seen, notification_status) "
            "VALUES ('123', 'old', 'old', 'unknown')"
        )
        conn.commit()

        assert reserve_delivery(conn, 3, "123", "first") == "ambiguous"
        assert resolve_ambiguous_delivery(conn, 3, "123", delivered=True, now="second")
        assert listing_exists(conn, 3, "123")
        assert reserve_delivery(conn, 3, "123", "third") == "sent"
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
