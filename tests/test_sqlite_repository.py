from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from weather_alpha.models.records import (
    DailyActualMaximum,
    ForecastEnsembleMember,
    HourlyObservation,
    MarketOutcome,
    NormalizedMarket,
    OrderBookLevel,
    OrderBookSnapshot,
    PriceSnapshot,
    Provenance,
    TradeRecord,
    WeatherForecast,
)
from weather_alpha.storage.repository import WeatherAlphaRepository
from weather_alpha.storage.schema import SCHEMA_VERSION


def _prov() -> Provenance:
    return Provenance(
        source="fixture",
        retrieved_at=datetime(2024, 7, 15, 12, 0, tzinfo=UTC),
        request_url="https://example.test/markets",
        raw_path="data/polymarket/example.json",
        content_sha256="abc123",
        limitations=(),
    )


def test_init_schema_and_idempotent_market_upsert(tmp_path: Path) -> None:
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    assert repo.schema_version() == SCHEMA_VERSION

    market = NormalizedMarket(
        condition_id="0x" + "aa" * 32,
        market_id="1",
        event_id="10",
        slug="highest-temperature-in-paris-on-july-15-2026",
        question="Highest temperature in Paris on July 15?",
        description="Resolves from Wunderground at Paris-Le Bourget (LFPB).",
        city="paris",
        station_icao="LFPB",
        event_date="2026-07-15",
        parse_status="resolved",
        parse_notes=(),
        closed=False,
        active=True,
        start_time=datetime(2026, 7, 14, 0, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 16, 0, 0, tzinfo=UTC),
        provenance=_prov(),
    )
    repo.upsert_market(market)
    repo.upsert_market(market)
    assert repo.count("markets") == 1

    outcome = MarketOutcome(
        condition_id=market.condition_id,
        token_id="token-yes-31c",
        outcome_label="Yes",
        outcome_index=0,
        temperature_celsius_min=31.0,
        temperature_celsius_max=31.0,
        bucket_kind="exact",
        group_item_title="31°C",
        provenance=_prov(),
    )
    repo.upsert_outcome(outcome)
    repo.upsert_outcome(outcome)
    assert repo.count("outcomes") == 1


def test_price_trade_and_orderbook_idempotency(tmp_path: Path) -> None:
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    ts = datetime(2024, 7, 15, 12, 0, tzinfo=UTC)
    snap = PriceSnapshot(
        token_id="token-yes-31c",
        condition_id="0x" + "aa" * 32,
        observed_at=ts,
        price=0.42,
        provenance=_prov(),
    )
    repo.upsert_price_snapshot(snap)
    repo.upsert_price_snapshot(snap)
    assert repo.count("price_snapshots") == 1

    trade = TradeRecord(
        trade_id="tx-1:0",
        transaction_hash="0xabc",
        condition_id="0x" + "aa" * 32,
        token_id="token-yes-31c",
        side="BUY",
        price=0.41,
        size=10.0,
        traded_at=ts,
        provenance=_prov(),
    )
    repo.upsert_trade(trade)
    repo.upsert_trade(trade)
    assert repo.count("trades") == 1

    book = OrderBookSnapshot(
        snapshot_id="book-1",
        token_id="token-yes-31c",
        condition_id="0x" + "aa" * 32,
        observed_at=ts,
        hash="h1",
        tick_size=0.01,
        min_order_size=1.0,
        last_trade_price=0.41,
        provenance=_prov(),
        levels=(
            OrderBookLevel(side="bid", price=0.40, size=100.0, level_index=0),
            OrderBookLevel(side="ask", price=0.44, size=50.0, level_index=0),
        ),
    )
    repo.upsert_order_book(book)
    repo.upsert_order_book(book)
    assert repo.count("order_book_snapshots") == 1
    assert repo.count("order_book_levels") == 2


def test_weather_tables_idempotent(tmp_path: Path) -> None:
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    valid_time = datetime(2024, 7, 15, 12, 0, tzinfo=UTC)
    forecast = WeatherForecast(
        station_id="LFPG",
        provider="open-meteo-historical-forecast",
        model="best_match",
        issued_at=None,
        valid_time=valid_time,
        variable="temperature_2m",
        temperature_celsius=27.1,
        source_value=27.1,
        source_unit="C",
        lead_hours=None,
        provenance=replace(
            _prov(),
            limitations=("historical_forecast_stitches_runs; issuance time not provided",),
        ),
    )
    repo.upsert_forecast(forecast)
    repo.upsert_forecast(forecast)
    assert repo.count("weather_forecasts") == 1

    member = ForecastEnsembleMember(
        station_id="LFPG",
        provider="open-meteo-ensemble",
        model="ecmwf_ifs025",
        issued_at=None,
        valid_time=valid_time,
        member_id="member01",
        variable="temperature_2m",
        temperature_celsius=26.4,
        source_value=26.4,
        source_unit="C",
        provenance=replace(
            _prov(),
            limitations=(
                "ensemble member issuance time is not provided by Open-Meteo Ensemble API",
            ),
        ),
    )
    repo.upsert_ensemble_member(member)
    repo.upsert_ensemble_member(member)
    assert repo.count("forecast_ensemble_members") == 1

    hourly = HourlyObservation(
        station_id="LFPG",
        provider="open-meteo-archive",
        observed_at=valid_time,
        variable="temperature_2m",
        temperature_celsius=28.0,
        source_value=28.0,
        source_unit="C",
        provenance=_prov(),
    )
    repo.upsert_hourly_observation(hourly)
    repo.upsert_hourly_observation(hourly)
    assert repo.count("hourly_observations") == 1

    daily = DailyActualMaximum(
        station_id="LFPG",
        provider="open-meteo-archive",
        local_date="2024-07-15",
        timezone_name="Europe/Paris",
        temperature_celsius=31.2,
        source_value=31.2,
        source_unit="C",
        provenance=_prov(),
    )
    repo.upsert_daily_maximum(daily)
    repo.upsert_daily_maximum(daily)
    assert repo.count("daily_actual_maxima") == 1


_LEGACY_V1_RAW_PAYLOADS = """
PRAGMA foreign_keys = ON;
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE raw_payloads (
    content_sha256 TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    request_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    disk_path TEXT NOT NULL,
    limitations_json TEXT NOT NULL
);
"""


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [
        row["name"] for row in sorted(info, key=lambda item: int(item["pk"])) if int(row["pk"]) > 0
    ]


def test_raw_payloads_same_digest_distinct_urls_both_remain_queryable(tmp_path: Path) -> None:
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    digest = "a" * 64
    ts = datetime(2024, 7, 15, 12, 0, tzinfo=UTC)
    url_a = "https://clob.polymarket.com/book?token_id=aaa"
    url_b = "https://clob.polymarket.com/book?token_id=bbb"
    repo.record_raw_payload(
        content_sha256=digest,
        source="polymarket/clob-book",
        request_url=url_a,
        retrieved_at=ts,
        disk_path="/raw/a.json",
    )
    repo.record_raw_payload(
        content_sha256=digest,
        source="polymarket/clob-book",
        request_url=url_b,
        retrieved_at=ts,
        disk_path="/raw/b.json",
    )
    repo.record_raw_payload(
        content_sha256=digest,
        source="polymarket/clob-book",
        request_url=url_a,
        retrieved_at=datetime(2024, 7, 15, 12, 5, tzinfo=UTC),
        disk_path="/raw/a.json",
    )
    assert repo.count("raw_payloads") == 2
    with repo.connect() as conn:
        rows = conn.execute(
            """
            SELECT request_url, disk_path, content_sha256
            FROM raw_payloads
            WHERE content_sha256 = ?
            ORDER BY request_url
            """,
            (digest,),
        ).fetchall()
    assert [(row["request_url"], row["disk_path"], row["content_sha256"]) for row in rows] == [
        (url_a, "/raw/a.json", digest),
        (url_b, "/raw/b.json", digest),
    ]


def test_migrate_raw_payloads_from_legacy_content_sha256_primary_key(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    digest = "b" * 64
    url_a = "https://gamma-api.polymarket.com/public-search?q=paris"
    url_b = "https://gamma-api.polymarket.com/markets?limit=20"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(_LEGACY_V1_RAW_PAYLOADS)
        conn.execute("INSERT INTO schema_meta(key, value) VALUES (?, ?)", ("schema_version", "1"))
        conn.execute(
            """
            INSERT INTO raw_payloads(
                content_sha256, source, request_url, retrieved_at, disk_path, limitations_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                digest,
                "polymarket/gamma-search",
                url_a,
                "2024-07-15T12:00:00+00:00",
                "/raw/legacy-a.json",
                "[]",
            ),
        )
        assert _pk_columns(conn, "raw_payloads") == ["content_sha256"]

    repo = WeatherAlphaRepository(db)
    repo.init_schema()
    assert SCHEMA_VERSION >= 2
    assert repo.schema_version() == SCHEMA_VERSION
    with repo.connect() as conn:
        assert _pk_columns(conn, "raw_payloads") == ["content_sha256", "request_url"]
        preserved = conn.execute(
            "SELECT request_url, disk_path, source FROM raw_payloads WHERE content_sha256 = ?",
            (digest,),
        ).fetchone()
        assert preserved is not None
        assert preserved["request_url"] == url_a
        assert preserved["disk_path"] == "/raw/legacy-a.json"
        assert preserved["source"] == "polymarket/gamma-search"

    repo.record_raw_payload(
        content_sha256=digest,
        source="polymarket/gamma-markets",
        request_url=url_b,
        retrieved_at=datetime(2024, 7, 15, 12, 1, tzinfo=UTC),
        disk_path="/raw/legacy-b.json",
    )
    repo.init_schema()
    assert repo.count("raw_payloads") == 2
    with repo.connect() as conn:
        urls = {
            row["request_url"]
            for row in conn.execute(
                "SELECT request_url, disk_path FROM raw_payloads WHERE content_sha256 = ?",
                (digest,),
            )
        }
        paths = {
            row["disk_path"]
            for row in conn.execute(
                "SELECT disk_path FROM raw_payloads WHERE content_sha256 = ?",
                (digest,),
            )
        }
    assert urls == {url_a, url_b}
    assert paths == {"/raw/legacy-a.json", "/raw/legacy-b.json"}
