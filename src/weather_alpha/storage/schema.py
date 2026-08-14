"""SQLite DDL for research storage."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    market_id TEXT,
    event_id TEXT,
    slug TEXT,
    question TEXT NOT NULL,
    description TEXT,
    city TEXT,
    station_icao TEXT,
    event_date TEXT,
    parse_status TEXT NOT NULL,
    parse_notes_json TEXT NOT NULL,
    closed INTEGER,
    active INTEGER,
    start_time TEXT,
    end_time TEXT,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    request_url TEXT,
    raw_path TEXT,
    content_sha256 TEXT,
    limitations_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome_label TEXT NOT NULL,
    outcome_index INTEGER,
    temperature_celsius_min REAL,
    temperature_celsius_max REAL,
    bucket_kind TEXT,
    group_item_title TEXT,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    request_url TEXT,
    raw_path TEXT,
    content_sha256 TEXT,
    limitations_json TEXT NOT NULL,
    PRIMARY KEY (condition_id, token_id)
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    token_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    condition_id TEXT,
    price REAL NOT NULL,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    request_url TEXT,
    raw_path TEXT,
    content_sha256 TEXT,
    limitations_json TEXT NOT NULL,
    PRIMARY KEY (token_id, observed_at)
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    transaction_hash TEXT,
    condition_id TEXT,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    traded_at TEXT NOT NULL,
    outcome_label TEXT,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    request_url TEXT,
    raw_path TEXT,
    content_sha256 TEXT,
    limitations_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_book_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    token_id TEXT NOT NULL,
    condition_id TEXT,
    observed_at TEXT NOT NULL,
    hash TEXT,
    tick_size REAL,
    min_order_size REAL,
    last_trade_price REAL,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    request_url TEXT,
    raw_path TEXT,
    content_sha256 TEXT,
    limitations_json TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_order_book_natural
    ON order_book_snapshots (token_id, observed_at, IFNULL(hash, ''));

CREATE TABLE IF NOT EXISTS order_book_levels (
    snapshot_id TEXT NOT NULL,
    side TEXT NOT NULL,
    level_index INTEGER NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    PRIMARY KEY (snapshot_id, side, level_index),
    FOREIGN KEY (snapshot_id) REFERENCES order_book_snapshots(snapshot_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS weather_forecasts (
    station_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    issued_at TEXT,
    valid_time TEXT NOT NULL,
    variable TEXT NOT NULL,
    temperature_celsius REAL,
    source_value REAL,
    source_unit TEXT,
    lead_hours REAL,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    request_url TEXT,
    raw_path TEXT,
    content_sha256 TEXT,
    limitations_json TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_weather_forecasts_natural
    ON weather_forecasts (
        station_id,
        provider,
        IFNULL(model, ''),
        IFNULL(issued_at, ''),
        valid_time,
        variable
    );

CREATE TABLE IF NOT EXISTS forecast_ensemble_members (
    station_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    issued_at TEXT,
    valid_time TEXT NOT NULL,
    member_id TEXT NOT NULL,
    variable TEXT NOT NULL,
    temperature_celsius REAL,
    source_value REAL,
    source_unit TEXT,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    request_url TEXT,
    raw_path TEXT,
    content_sha256 TEXT,
    limitations_json TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ensemble_natural
    ON forecast_ensemble_members (
        station_id,
        provider,
        IFNULL(model, ''),
        IFNULL(issued_at, ''),
        valid_time,
        member_id,
        variable
    );

CREATE TABLE IF NOT EXISTS hourly_observations (
    station_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    variable TEXT NOT NULL,
    temperature_celsius REAL,
    source_value REAL,
    source_unit TEXT,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    request_url TEXT,
    raw_path TEXT,
    content_sha256 TEXT,
    limitations_json TEXT NOT NULL,
    PRIMARY KEY (station_id, provider, observed_at, variable)
);

CREATE TABLE IF NOT EXISTS daily_actual_maxima (
    station_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    local_date TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    temperature_celsius REAL,
    source_value REAL,
    source_unit TEXT,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    request_url TEXT,
    raw_path TEXT,
    content_sha256 TEXT,
    limitations_json TEXT NOT NULL,
    PRIMARY KEY (station_id, provider, local_date)
);

CREATE TABLE IF NOT EXISTS raw_payloads (
    content_sha256 TEXT NOT NULL,
    source TEXT NOT NULL,
    request_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    disk_path TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    PRIMARY KEY (content_sha256, request_url)
);
"""


def apply_migrations(conn: sqlite3.Connection) -> None:
    _migrate_raw_payloads_composite_identity(conn)


def _migrate_raw_payloads_composite_identity(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'raw_payloads'"
    ).fetchone()
    if exists is None:
        return
    pk_columns = _primary_key_columns(conn, "raw_payloads")
    if pk_columns == ["content_sha256", "request_url"]:
        return
    conn.executescript(
        """
        CREATE TABLE raw_payloads_v2 (
            content_sha256 TEXT NOT NULL,
            source TEXT NOT NULL,
            request_url TEXT NOT NULL,
            retrieved_at TEXT NOT NULL,
            disk_path TEXT NOT NULL,
            limitations_json TEXT NOT NULL,
            PRIMARY KEY (content_sha256, request_url)
        );
        INSERT INTO raw_payloads_v2(
            content_sha256, source, request_url, retrieved_at, disk_path, limitations_json
        )
        SELECT content_sha256, source, request_url, retrieved_at, disk_path, limitations_json
        FROM raw_payloads;
        DROP TABLE raw_payloads;
        ALTER TABLE raw_payloads_v2 RENAME TO raw_payloads;
        """
    )


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    keyed: list[tuple[int, str]] = []
    for row in info:
        pk = int(row["pk"])
        if pk > 0:
            keyed.append((pk, str(row["name"])))
    keyed.sort()
    return [name for _, name in keyed]
