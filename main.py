"""Configuration validation and SQLite persistence for the notifier."""

import json
import os
import sqlite3
from pathlib import Path

import yaml

from crawler import (
    REGIONS,
    _resolve_kinds,
    _resolve_region,
    _resolve_sections,
    _validate_price_range,
)

DEFAULT_DB = "listings.db"

LISTING_COLUMNS = (
    "id", "title", "url", "image", "price", "price_value", "tags", "kind",
    "layout", "area", "floor", "community", "location", "nearby_transit",
    "poster", "updated", "views", "region", "section", "first_seen",
    "last_seen", "raw",
)

TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    id             TEXT PRIMARY KEY,
    title          TEXT,
    url            TEXT,
    image          TEXT,
    price          TEXT,
    price_value    INTEGER,
    tags           TEXT,  -- JSON array
    kind           TEXT,
    layout         TEXT,
    area           TEXT,
    floor          TEXT,
    community      TEXT,
    location       TEXT,
    nearby_transit TEXT,  -- JSON object
    poster         TEXT,
    updated        TEXT,
    views          TEXT,
    region         TEXT,
    section        TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    raw            TEXT   -- full listing JSON
)
"""


def _table_name(region):
    """Return the SQLite table name for a validated region id or name."""
    region_id = _resolve_region(region)
    return f"listings_{REGIONS[region_id]}"


def _quote_identifier(identifier):
    """Safely quote a SQLite identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _ensure_region_table(conn, region):
    """Create and return the quoted listings table for one region."""
    table = _table_name(region)
    conn.execute(TABLE_SCHEMA.format(table=_quote_identifier(table)))
    return _quote_identifier(table)


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _migrate_legacy_table(conn):
    """Move the former combined `listings` table into per-region tables."""
    if not _table_exists(conn, "listings"):
        return

    legacy_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(listings)")
    }
    if "region" not in legacy_columns:
        raise ValueError("legacy listings table has no 'region' column")

    if "section" not in legacy_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN section TEXT")
        legacy_columns.add("section")
    if "location" in legacy_columns:
        conn.execute(
            """
            UPDATE listings
            SET section = CASE
                WHEN instr(location, '-') > 0
                    THEN substr(location, 1, instr(location, '-') - 1)
                ELSE location
            END
            WHERE section IS NULL AND location IS NOT NULL
            """
        )

    region_names = [
        row[0]
        for row in conn.execute("SELECT DISTINCT region FROM listings")
    ]
    unknown = [name for name in region_names if name not in REGIONS.values()]
    if unknown:
        raise ValueError(f"cannot migrate unknown regions: {unknown}")

    copy_columns = [c for c in LISTING_COLUMNS if c in legacy_columns]
    column_sql = ", ".join(_quote_identifier(c) for c in copy_columns)
    for region in region_names:
        table = _ensure_region_table(conn, region)
        conn.execute(
            f"INSERT OR IGNORE INTO {table} ({column_sql}) "
            f"SELECT {column_sql} FROM listings WHERE region = ?",
            (region,),
        )

    legacy_count = conn.execute("SELECT count(*) FROM listings").fetchone()[0]
    migrated_count = sum(
        conn.execute(
            f"SELECT count(*) FROM listings AS old "
            f"JOIN {_quote_identifier(_table_name(region))} AS new USING (id) "
            "WHERE old.region = ?",
            (region,),
        ).fetchone()[0]
        for region in region_names
    )
    if migrated_count != legacy_count:
        raise RuntimeError(
            f"legacy migration copied {migrated_count} of {legacy_count} listings"
        )
    conn.execute("DROP TABLE listings")


def load_config(path):
    """Load and validate the YAML config.

    Returns a database path and normalized crawl jobs.
    Raises ValueError on invalid config.
    """
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or not isinstance(cfg.get("crawl"), list) or not cfg["crawl"]:
        raise ValueError("config must define a non-empty 'crawl' list")

    jobs = []
    for entry in cfg["crawl"]:
        if not isinstance(entry, dict) or "region" not in entry:
            raise ValueError("each crawl entry must define 'region'")
        region_id = _resolve_region(entry["region"])  # validates id/name
        section_ids = _resolve_sections(region_id, entry.get("sections"))
        kind_ids = _resolve_kinds(entry.get("kinds"))
        price = entry.get("price", {})
        if price is None:
            price = {}
        if not isinstance(price, dict):
            raise ValueError("'price' must define optional 'min' and 'max' values")
        unknown_price_keys = set(price) - {"min", "max"}
        if unknown_price_keys:
            raise ValueError(
                f"unknown 'price' options: {sorted(unknown_price_keys)}"
            )
        price_min, price_max = _validate_price_range(
            price.get("min"), price.get("max")
        )
        if entry.get("pages", 1) != 1:
            raise ValueError("'pages' is fixed at 1 (maximum 30 results per county)")
        jobs.append({
            "region_id": region_id,
            "section_ids": section_ids,
            "kind_ids": kind_ids,
            "price_min": price_min,
            "price_max": price_max,
        })

    database = os.getenv("DATABASE_PATH", cfg.get("database", DEFAULT_DB))
    if not isinstance(database, str) or not database.strip():
        raise ValueError("'database' must be a non-empty path")

    return {"database": database, "jobs": jobs}


def init_db(db_path, regions=None):
    """Open SQLite, migrate legacy data, and create requested region tables."""
    if db_path != ":memory:":
        db_path = Path(db_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        _migrate_legacy_table(conn)
        for region in regions or []:
            _ensure_region_table(conn, region)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return conn


def listing_exists(conn, region, listing_id):
    """Return whether a listing has already been successfully notified."""
    table = _ensure_region_table(conn, region)
    return conn.execute(
        f"SELECT 1 FROM {table} WHERE id = ?", (str(listing_id),)
    ).fetchone() is not None


def insert_notified_listing(conn, listing, region, now):
    """Store one listing after notification; return False if it already exists."""
    region_name = REGIONS[_resolve_region(region)]
    table = _ensure_region_table(conn, region_name)
    location = listing.get("location", "")
    section = location.split("-", 1)[0] if location else ""
    cursor = conn.execute(
        f"""
        INSERT INTO {table} (
            id, title, url, image, price, price_value, tags, kind, layout,
            area, floor, community, location, nearby_transit, poster,
            updated, views, region, section, first_seen, last_seen, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            listing["id"],
            listing.get("title", ""),
            listing.get("url", ""),
            listing.get("image", ""),
            listing.get("price", ""),
            listing.get("price_value"),
            json.dumps(listing.get("tags", []), ensure_ascii=False),
            listing.get("kind", ""),
            listing.get("layout", ""),
            listing.get("area", ""),
            listing.get("floor", ""),
            listing.get("community", ""),
            location,
            json.dumps(listing.get("nearby_transit", {}), ensure_ascii=False),
            listing.get("poster", ""),
            listing.get("updated", ""),
            listing.get("views", ""),
            region_name,
            section,
            now,
            now,
            json.dumps(listing, ensure_ascii=False),
        ),
    )
    conn.commit()
    return cursor.rowcount == 1
