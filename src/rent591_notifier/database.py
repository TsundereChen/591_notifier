"""Configuration validation and SQLite persistence for the notifier."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .crawler import (
    REGIONS,
    _resolve_kinds,
    _resolve_region,
    _resolve_sections,
    _validate_price_range,
)

DEFAULT_DB = "listings.db"
SCHEMA_VERSION = 3
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryReservation:
    """Result of reserving one delivery attempt."""

    status: str
    attempt_count: int | None = None


BASE_LISTING_COLUMNS = (
    "id",
    "title",
    "url",
    "image",
    "price",
    "price_value",
    "tags",
    "kind",
    "layout",
    "area",
    "floor",
    "community",
    "location",
    "nearby_transit",
    "poster",
    "updated",
    "views",
    "region",
    "section",
    "first_seen",
    "last_seen",
    "raw",
)

TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    id                  TEXT PRIMARY KEY,
    title               TEXT,
    url                 TEXT,
    image               TEXT,
    price               TEXT,
    price_value         INTEGER,
    tags                TEXT,
    kind                TEXT,
    layout              TEXT,
    area                TEXT,
    floor               TEXT,
    community           TEXT,
    location            TEXT,
    nearby_transit      TEXT,
    poster              TEXT,
    updated             TEXT,
    views               TEXT,
    region              TEXT,
    section             TEXT,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    raw                 TEXT,
    notification_status TEXT NOT NULL DEFAULT 'sent'
        CHECK (notification_status IN ('sent', 'unknown')),
    notified_at         TEXT,
    telegram_chat_id    INTEGER,
    telegram_message_id INTEGER
)
"""

DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_attempts (
    region_id           INTEGER NOT NULL,
    listing_id          TEXT NOT NULL,
    status              TEXT NOT NULL
        CHECK (status IN ('sending', 'sent', 'ambiguous')),
    started_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    telegram_chat_id    INTEGER,
    telegram_message_id INTEGER,
    payload             TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    PRIMARY KEY (region_id, listing_id)
)
"""


def _table_name(region: int | str) -> str:
    """Return the SQLite table name for a validated region id or name."""
    region_id = _resolve_region(region)
    return f"listings_{REGIONS[region_id]}"


def _quote_identifier(identifier: str) -> str:
    """Safely quote a SQLite identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1] for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    }


def _ensure_delivery_columns(conn: sqlite3.Connection) -> None:
    """Upgrade an existing delivery ledger without discarding retry state."""
    columns = _table_columns(conn, "delivery_attempts")
    additions = {
        "payload": "TEXT",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(
                "ALTER TABLE delivery_attempts "
                f"ADD COLUMN {_quote_identifier(name)} {declaration}"
            )


def _ensure_region_table(conn: sqlite3.Connection, region: int | str) -> str:
    """Create/upgrade and return the quoted listings table for one region."""
    table = _table_name(region)
    existed = _table_exists(conn, table)
    quoted = _quote_identifier(table)
    conn.execute(TABLE_SCHEMA.format(table=quoted))

    # Tables produced before notification tracking have no proof that their
    # rows were delivered. Preserve them as unknown so reserve_delivery() can
    # treat them as incomplete deliveries and retry them.
    if existed:
        columns = _table_columns(conn, table)
        additions = {
            "notification_status": (
                "TEXT NOT NULL DEFAULT 'unknown' "
                "CHECK (notification_status IN ('sent', 'unknown'))"
            ),
            "notified_at": "TEXT",
            "telegram_chat_id": "INTEGER",
            "telegram_message_id": "INTEGER",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE {quoted} ADD COLUMN {_quote_identifier(name)} "
                    f"{declaration}"
                )
    return quoted


def _legacy_conflict(
    conn: sqlite3.Connection,
    region: str,
    target_table: str,
    columns: Sequence[str],
) -> str | None:
    comparable = [column for column in columns if column not in {"region"}]
    if not comparable:
        return None
    differs = " OR ".join(
        f"NOT (old.{_quote_identifier(column)} IS new.{_quote_identifier(column)})"
        for column in comparable
    )
    row = conn.execute(
        f"SELECT old.id FROM listings AS old "
        f"JOIN {target_table} AS new USING (id) "
        f"WHERE old.region = ? AND ({differs}) LIMIT 1",
        (region,),
    ).fetchone()
    return str(row[0]) if row else None


def _migrate_legacy_table(conn: sqlite3.Connection) -> None:
    """Move the former combined table without discarding conflicting rows."""
    if not _table_exists(conn, "listings"):
        return

    legacy_columns = _table_columns(conn, "listings")
    if "region" not in legacy_columns:
        raise ValueError("legacy listings table has no 'region' column")

    if "section" not in legacy_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN section TEXT")
        legacy_columns.add("section")
    if "location" in legacy_columns:
        conn.execute("""
            UPDATE listings
            SET section = CASE
                WHEN instr(location, '-') > 0
                    THEN substr(location, 1, instr(location, '-') - 1)
                ELSE location
            END
            WHERE section IS NULL AND location IS NOT NULL
            """)

    region_names = [
        row[0] for row in conn.execute("SELECT DISTINCT region FROM listings")
    ]
    unknown = [name for name in region_names if name not in REGIONS.values()]
    if unknown:
        raise ValueError(f"cannot migrate unknown regions: {unknown}")

    copy_columns = [
        column for column in BASE_LISTING_COLUMNS if column in legacy_columns
    ]
    column_sql = ", ".join(_quote_identifier(column) for column in copy_columns)
    for region in region_names:
        target = _ensure_region_table(conn, region)
        conflict_id = _legacy_conflict(conn, region, target, copy_columns)
        if conflict_id is not None:
            raise RuntimeError(
                f"legacy migration conflict in {region} for listing {conflict_id}; "
                "the legacy table was preserved"
            )
        conn.execute(
            f"INSERT OR IGNORE INTO {target} ({column_sql}, notification_status) "
            f"SELECT {column_sql}, 'unknown' FROM listings WHERE region = ?",
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


def resolve_database_path(
    config_path: str | os.PathLike[str], configured_database: str
) -> str:
    """Resolve the effective DB path relative to its YAML file."""
    database = os.getenv("DATABASE_PATH", configured_database)
    if not isinstance(database, str) or not database.strip():
        raise ValueError("'database' must be a non-empty path")
    if database == ":memory:":
        return database
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = Path(config_path).expanduser().resolve().parent / path
    return str(path.resolve())


def load_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load and validate YAML, resolving relative DB paths beside the config."""
    config_path = Path(path).expanduser().resolve()
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if (
        not isinstance(cfg, dict)
        or not isinstance(cfg.get("crawl"), list)
        or not cfg["crawl"]
    ):
        raise ValueError("config must define a non-empty 'crawl' list")

    jobs = []
    seen_regions: set[int] = set()
    for entry in cfg["crawl"]:
        if not isinstance(entry, dict) or "region" not in entry:
            raise ValueError("each crawl entry must define 'region'")
        region_id = _resolve_region(entry["region"])
        if region_id in seen_regions:
            raise ValueError(
                f"duplicate crawl region {REGIONS[region_id]!r}; combine its filters"
            )
        seen_regions.add(region_id)
        section_ids = _resolve_sections(region_id, entry.get("sections"))
        kind_ids = _resolve_kinds(entry.get("kinds"))
        price = entry.get("price", {})
        if price is None:
            price = {}
        if not isinstance(price, dict):
            raise ValueError("'price' must define optional 'min' and 'max' values")
        unknown_price_keys = set(price) - {"min", "max"}
        if unknown_price_keys:
            raise ValueError(f"unknown 'price' options: {sorted(unknown_price_keys)}")
        price_min, price_max = _validate_price_range(price.get("min"), price.get("max"))
        # `pages` is no longer configurable. Accept the former value for
        # backward compatibility, but all runs now query five pages.
        if entry.get("pages", 5) not in (1, 5):
            raise ValueError("'pages' is fixed at 5 (maximum 150 results per county)")
        jobs.append(
            {
                "region_id": region_id,
                "section_ids": section_ids,
                "kind_ids": kind_ids,
                "price_min": price_min,
                "price_max": price_max,
            }
        )

    database = resolve_database_path(config_path, cfg.get("database", DEFAULT_DB))

    return {"database": database, "jobs": jobs}


def init_db(
    db_path: str | os.PathLike[str], regions: Iterable[int | str] | None = None
) -> sqlite3.Connection:
    """Open SQLite, run transactional migrations, and tune safe concurrency."""
    regions = tuple(regions or ())
    file_database = str(db_path) != ":memory:"
    if file_database:
        path = Path(db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        db_path = path
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    if file_database:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    try:
        with conn:
            _migrate_legacy_table(conn)
            conn.execute(DELIVERY_SCHEMA)
            _ensure_delivery_columns(conn)
            for region in regions:
                _ensure_region_table(conn, region)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except Exception:
        LOGGER.exception(
            "Database initialization or migration failed database=%s regions=%s",
            db_path,
            list(regions),
        )
        conn.close()
        raise
    LOGGER.info(
        "Database ready database=%s regions=%s schema_version=%s",
        db_path,
        list(regions),
        SCHEMA_VERSION,
    )
    return conn


def listing_exists(
    conn: sqlite3.Connection,
    region: int | str,
    listing_id: str | int,
    *,
    seen_at: str | None = None,
) -> bool:
    """Return whether a listing is proven delivered, optionally touching last_seen."""
    table = _ensure_region_table(conn, region)
    exists = _notified_exists_in_table(conn, table, listing_id)
    if exists and seen_at is not None:
        with conn:
            conn.execute(
                f"UPDATE {table} SET last_seen = ? WHERE id = ?",
                (seen_at, str(listing_id)),
            )
    return exists


def _notified_exists_in_table(
    conn: sqlite3.Connection, table: str, listing_id: str | int
) -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM {table} WHERE id = ? AND notification_status = 'sent'",
            (str(listing_id),),
        ).fetchone()
        is not None
    )


def notified_listing_ids(
    conn: sqlite3.Connection,
    region: int | str,
    listing_ids: Sequence[str],
    *,
    seen_at: str,
) -> set[str]:
    """Fetch delivered IDs for a page in one query and update last_seen."""
    if not listing_ids:
        return set()
    table = _ensure_region_table(conn, region)
    placeholders = ", ".join("?" for _ in listing_ids)
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE notification_status = 'sent' "
        f"AND id IN ({placeholders})",
        tuple(listing_ids),
    ).fetchall()
    found = {str(row[0]) for row in rows}
    with conn:
        conn.execute(
            f"UPDATE {table} SET last_seen = ? WHERE id IN ({placeholders})",
            (seen_at, *listing_ids),
        )
    return found


def reserve_delivery(
    conn: sqlite3.Connection,
    region: int | str,
    listing_id: str | int,
    now: str,
    *,
    listing: dict[str, Any] | None = None,
) -> DeliveryReservation:
    """Atomically reserve an ID, retrying incomplete attempts by default."""
    region_id = _resolve_region(region)
    listing_id = str(listing_id)
    payload = json.dumps(listing, ensure_ascii=False) if listing is not None else None
    table = _quote_identifier(_table_name(region_id))
    listing_row = conn.execute(
        f"SELECT notification_status FROM {table} WHERE id = ?", (listing_id,)
    ).fetchone()
    if listing_row and listing_row[0] == "sent":
        with conn:
            conn.execute(
                "UPDATE delivery_attempts SET status = 'sent', updated_at = ?, "
                "last_error = NULL WHERE region_id = ? AND listing_id = ? "
                "AND status != 'sent'",
                (now, region_id, listing_id),
            )
        return DeliveryReservation("sent")

    with conn:
        row = conn.execute(
            "SELECT status, attempt_count FROM delivery_attempts "
            "WHERE region_id = ? AND listing_id = ?",
            (region_id, listing_id),
        ).fetchone()
        if row:
            status = str(row[0])
            if status == "sent":
                return DeliveryReservation("sent")
            attempt_count = int(row[1]) + 1
            conn.execute(
                "UPDATE delivery_attempts SET status = 'sending', started_at = ?, "
                "updated_at = ?, payload = COALESCE(?, payload), "
                "attempt_count = ?, last_error = NULL "
                "WHERE region_id = ? AND listing_id = ?",
                (now, now, payload, attempt_count, region_id, listing_id),
            )
            return DeliveryReservation("reserved", attempt_count)
        if listing_row and listing_row[0] == "unknown":
            conn.execute(
                "INSERT INTO delivery_attempts "
                "(region_id, listing_id, status, started_at, updated_at, payload, "
                "attempt_count) VALUES (?, ?, 'sending', ?, ?, ?, 1)",
                (region_id, listing_id, now, now, payload),
            )
            return DeliveryReservation("reserved", 1)
        conn.execute(
            "INSERT INTO delivery_attempts "
            "(region_id, listing_id, status, started_at, updated_at, payload, "
            "attempt_count) VALUES (?, ?, 'sending', ?, ?, ?, 1)",
            (region_id, listing_id, now, now, payload),
        )
    return DeliveryReservation("reserved", 1)


def mark_delivery_ambiguous(
    conn: sqlite3.Connection,
    region: int | str,
    listing_id: str | int,
    now: str,
    *,
    attempt_count: int,
    error: str,
) -> bool:
    """Record a failed/uncertain send for retry during the next crawl."""
    with conn:
        cursor = conn.execute(
            "UPDATE delivery_attempts SET status = 'ambiguous', updated_at = ?, "
            "last_error = ? WHERE region_id = ? AND listing_id = ? "
            "AND status = 'sending' AND attempt_count = ?",
            (
                now,
                error[:500],
                _resolve_region(region),
                str(listing_id),
                attempt_count,
            ),
        )
    return cursor.rowcount == 1


def delivery_status(
    conn: sqlite3.Connection, region: int | str, listing_id: str | int
) -> str | None:
    row = conn.execute(
        "SELECT status FROM delivery_attempts WHERE region_id = ? AND listing_id = ?",
        (_resolve_region(region), str(listing_id)),
    ).fetchone()
    return str(row[0]) if row else None


def ambiguous_deliveries(
    conn: sqlite3.Connection, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Return completed uncertain attempts for explicit owner reconciliation."""
    rows = conn.execute(
        "SELECT region_id, listing_id, status, started_at, updated_at, "
        "attempt_count, last_error FROM delivery_attempts WHERE status = 'ambiguous' "
        "ORDER BY updated_at LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "region_id": int(row[0]),
            "region": REGIONS[int(row[0])],
            "listing_id": str(row[1]),
            "status": str(row[2]),
            "started_at": str(row[3]),
            "updated_at": str(row[4]),
            "attempt_count": int(row[5]),
            "last_error": str(row[6]) if row[6] is not None else None,
        }
        for row in rows
    ]


def retryable_deliveries(
    conn: sqlite3.Connection, region: int | str
) -> list[dict[str, Any]]:
    """Return incomplete attempts, including sends interrupted by a crash."""
    region_id = _resolve_region(region)
    rows = conn.execute(
        "SELECT listing_id, status, payload, attempt_count, started_at, updated_at "
        "FROM delivery_attempts WHERE region_id = ? "
        "AND status IN ('sending', 'ambiguous') ORDER BY updated_at",
        (region_id,),
    ).fetchall()
    result = []
    for row in rows:
        listing = None
        if row[2] is not None:
            try:
                candidate = json.loads(str(row[2]))
            except (TypeError, ValueError):
                LOGGER.warning(
                    "Could not decode saved retry payload region=%s listing_id=%s; "
                    "using fallback notification",
                    REGIONS[region_id],
                    row[0],
                )
                candidate = None
            if isinstance(candidate, dict):
                listing = candidate
        result.append(
            {
                "region_id": region_id,
                "listing_id": str(row[0]),
                "status": str(row[1]),
                "listing": listing,
                "attempt_count": int(row[3]),
                "started_at": str(row[4]),
                "updated_at": str(row[5]),
            }
        )
    return result


def resolve_ambiguous_delivery(
    conn: sqlite3.Connection,
    region: int | str,
    listing_id: str | int,
    *,
    delivered: bool,
    now: str,
) -> bool:
    """Mark an ambiguous delivery as received or leave it queued for retry."""
    region_id = _resolve_region(region)
    listing_id = str(listing_id)
    table = _ensure_region_table(conn, region_id)
    with conn:
        if delivered:
            cursor = conn.execute(
                "UPDATE delivery_attempts SET status = 'sent', updated_at = ?, "
                "last_error = NULL "
                "WHERE region_id = ? AND listing_id = ? "
                "AND status = 'ambiguous'",
                (now, region_id, listing_id),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    f"UPDATE {table} SET notification_status = 'sent', "
                    "notified_at = COALESCE(notified_at, ?) "
                    "WHERE id = ? AND notification_status = 'unknown'",
                    (now, listing_id),
                )
        else:
            cursor = conn.execute(
                "UPDATE delivery_attempts SET updated_at = ? "
                "WHERE region_id = ? AND listing_id = ? AND status = 'ambiguous'",
                (now, region_id, listing_id),
            )
    return cursor.rowcount == 1


def _listing_values(
    listing: dict[str, Any], region_name: str, now: str
) -> tuple[Any, ...]:
    location = listing.get("location", "")
    section = location.split("-", 1)[0] if location else ""
    return (
        str(listing["id"]),
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
    )


def record_filtered_listing(
    conn: sqlite3.Connection,
    region: int | str,
    listing: dict[str, Any],
    now: str,
) -> bool:
    """Record a listing filtered out before notification (e.g. by AI).

    The row is stored with notification_status 'sent' and no Telegram receipt,
    meaning "handled without notification": it will neither be notified nor
    evaluated again.
    """
    return insert_notified_listing(conn, listing, region, now, attempt_count=0)


def insert_notified_listing(
    conn: sqlite3.Connection,
    listing: dict[str, Any],
    region: int | str,
    now: str,
    *,
    telegram_chat_id: int | None = None,
    telegram_message_id: int | None = None,
    attempt_count: int = 1,
) -> bool:
    """Atomically record a successful notification and finish its ledger row."""
    region_id = _resolve_region(region)
    region_name = REGIONS[region_id]
    table = _ensure_region_table(conn, region_name)
    listing_id = str(listing["id"])
    already_sent = _notified_exists_in_table(conn, table, listing_id)
    values = _listing_values(listing, region_name, now)
    columns = ", ".join(_quote_identifier(column) for column in BASE_LISTING_COLUMNS)
    placeholders = ", ".join("?" for _ in BASE_LISTING_COLUMNS)
    update_columns = [
        column for column in BASE_LISTING_COLUMNS if column not in {"id", "first_seen"}
    ]
    updates = ", ".join(
        f"{_quote_identifier(column)} = excluded.{_quote_identifier(column)}"
        for column in update_columns
    )
    with conn:
        conn.execute(
            f"""
            INSERT INTO {table} (
                {columns}, notification_status, notified_at,
                telegram_chat_id, telegram_message_id
            ) VALUES ({placeholders}, 'sent', ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                {updates},
                notification_status = 'sent',
                notified_at = excluded.notified_at,
                telegram_chat_id = excluded.telegram_chat_id,
                telegram_message_id = excluded.telegram_message_id
            """,
            (*values, now, telegram_chat_id, telegram_message_id),
        )
        conn.execute(
            "INSERT INTO delivery_attempts "
            "(region_id, listing_id, status, started_at, updated_at, "
            "telegram_chat_id, telegram_message_id, payload, attempt_count, last_error) "
            "VALUES (?, ?, 'sent', ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(region_id, listing_id) DO UPDATE SET "
            "status = 'sent', updated_at = excluded.updated_at, "
            "telegram_chat_id = excluded.telegram_chat_id, "
            "telegram_message_id = excluded.telegram_message_id, "
            "payload = excluded.payload, "
            "attempt_count = MAX(attempt_count, excluded.attempt_count), "
            "last_error = NULL",
            (
                region_id,
                listing_id,
                now,
                now,
                telegram_chat_id,
                telegram_message_id,
                json.dumps(listing, ensure_ascii=False),
                attempt_count,
            ),
        )
    return not already_sent
