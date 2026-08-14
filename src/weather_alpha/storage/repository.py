"""SQLite repository with idempotent upserts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_alpha.models.records import (
    DailyActualMaximum,
    ForecastEnsembleMember,
    HourlyObservation,
    MarketOutcome,
    NormalizedMarket,
    OrderBookSnapshot,
    PriceSnapshot,
    Provenance,
    TradeRecord,
    WeatherForecast,
)
from weather_alpha.storage.schema import DDL, SCHEMA_VERSION, apply_migrations


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _json(value: tuple[str, ...] | list[str]) -> str:
    return json.dumps(list(value), ensure_ascii=True)


def _prov_cols(provenance: Provenance) -> dict[str, Any]:
    return {
        "source": provenance.source,
        "retrieved_at": _iso(provenance.retrieved_at),
        "request_url": provenance.request_url,
        "raw_path": provenance.raw_path,
        "content_sha256": provenance.content_sha256,
        "limitations_json": _json(provenance.limitations),
    }


class WeatherAlphaRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(DDL)
            apply_migrations(conn)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?", ("schema_version",)
            ).fetchone()
        if row is None:
            raise RuntimeError("schema is not initialized")
        return int(row["value"])

    def count(self, table: str) -> int:
        allowed = {
            "markets",
            "outcomes",
            "price_snapshots",
            "trades",
            "order_book_snapshots",
            "order_book_levels",
            "weather_forecasts",
            "forecast_ensemble_members",
            "hourly_observations",
            "daily_actual_maxima",
            "raw_payloads",
        }
        if table not in allowed:
            raise ValueError(f"unknown table: {table}")
        with self.connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])

    def inspect_counts(self) -> dict[str, int]:
        tables = (
            "markets",
            "outcomes",
            "price_snapshots",
            "trades",
            "order_book_snapshots",
            "order_book_levels",
            "weather_forecasts",
            "forecast_ensemble_members",
            "hourly_observations",
            "daily_actual_maxima",
            "raw_payloads",
        )
        return {table: self.count(table) for table in tables}

    def upsert_market(self, market: NormalizedMarket) -> None:
        cols = _prov_cols(market.provenance)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO markets(
                    condition_id, market_id, event_id, slug, question, description,
                    city, station_icao, event_date, parse_status, parse_notes_json,
                    closed, active, start_time, end_time,
                    source, retrieved_at, request_url, raw_path, content_sha256, limitations_json
                ) VALUES (
                    :condition_id, :market_id, :event_id, :slug, :question, :description,
                    :city, :station_icao, :event_date, :parse_status, :parse_notes_json,
                    :closed, :active, :start_time, :end_time,
                    :source, :retrieved_at, :request_url, :raw_path, :content_sha256, :limitations_json
                )
                ON CONFLICT(condition_id) DO UPDATE SET
                    market_id=excluded.market_id,
                    event_id=excluded.event_id,
                    slug=excluded.slug,
                    question=excluded.question,
                    description=excluded.description,
                    city=excluded.city,
                    station_icao=excluded.station_icao,
                    event_date=excluded.event_date,
                    parse_status=excluded.parse_status,
                    parse_notes_json=excluded.parse_notes_json,
                    closed=excluded.closed,
                    active=excluded.active,
                    start_time=excluded.start_time,
                    end_time=excluded.end_time,
                    source=excluded.source,
                    retrieved_at=excluded.retrieved_at,
                    request_url=excluded.request_url,
                    raw_path=excluded.raw_path,
                    content_sha256=excluded.content_sha256,
                    limitations_json=excluded.limitations_json
                """,
                {
                    "condition_id": market.condition_id,
                    "market_id": market.market_id,
                    "event_id": market.event_id,
                    "slug": market.slug,
                    "question": market.question,
                    "description": market.description,
                    "city": market.city,
                    "station_icao": market.station_icao,
                    "event_date": market.event_date,
                    "parse_status": market.parse_status,
                    "parse_notes_json": _json(market.parse_notes),
                    "closed": None if market.closed is None else int(market.closed),
                    "active": None if market.active is None else int(market.active),
                    "start_time": _iso(market.start_time),
                    "end_time": _iso(market.end_time),
                    **cols,
                },
            )

    def upsert_outcome(self, outcome: MarketOutcome) -> None:
        cols = _prov_cols(outcome.provenance)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO outcomes(
                    condition_id, token_id, outcome_label, outcome_index,
                    temperature_celsius_min, temperature_celsius_max, bucket_kind,
                    group_item_title, source, retrieved_at, request_url, raw_path,
                    content_sha256, limitations_json
                ) VALUES (
                    :condition_id, :token_id, :outcome_label, :outcome_index,
                    :temperature_celsius_min, :temperature_celsius_max, :bucket_kind,
                    :group_item_title, :source, :retrieved_at, :request_url, :raw_path,
                    :content_sha256, :limitations_json
                )
                ON CONFLICT(condition_id, token_id) DO UPDATE SET
                    outcome_label=excluded.outcome_label,
                    outcome_index=excluded.outcome_index,
                    temperature_celsius_min=excluded.temperature_celsius_min,
                    temperature_celsius_max=excluded.temperature_celsius_max,
                    bucket_kind=excluded.bucket_kind,
                    group_item_title=excluded.group_item_title,
                    source=excluded.source,
                    retrieved_at=excluded.retrieved_at,
                    request_url=excluded.request_url,
                    raw_path=excluded.raw_path,
                    content_sha256=excluded.content_sha256,
                    limitations_json=excluded.limitations_json
                """,
                {
                    "condition_id": outcome.condition_id,
                    "token_id": outcome.token_id,
                    "outcome_label": outcome.outcome_label,
                    "outcome_index": outcome.outcome_index,
                    "temperature_celsius_min": outcome.temperature_celsius_min,
                    "temperature_celsius_max": outcome.temperature_celsius_max,
                    "bucket_kind": outcome.bucket_kind,
                    "group_item_title": outcome.group_item_title,
                    **cols,
                },
            )

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None:
        cols = _prov_cols(snapshot.provenance)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO price_snapshots(
                    token_id, observed_at, condition_id, price,
                    source, retrieved_at, request_url, raw_path, content_sha256, limitations_json
                ) VALUES (
                    :token_id, :observed_at, :condition_id, :price,
                    :source, :retrieved_at, :request_url, :raw_path, :content_sha256, :limitations_json
                )
                ON CONFLICT(token_id, observed_at) DO UPDATE SET
                    condition_id=excluded.condition_id,
                    price=excluded.price,
                    source=excluded.source,
                    retrieved_at=excluded.retrieved_at,
                    request_url=excluded.request_url,
                    raw_path=excluded.raw_path,
                    content_sha256=excluded.content_sha256,
                    limitations_json=excluded.limitations_json
                """,
                {
                    "token_id": snapshot.token_id,
                    "observed_at": _iso(snapshot.observed_at),
                    "condition_id": snapshot.condition_id,
                    "price": snapshot.price,
                    **cols,
                },
            )

    def upsert_trade(self, trade: TradeRecord) -> None:
        cols = _prov_cols(trade.provenance)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO trades(
                    trade_id, transaction_hash, condition_id, token_id, side, price, size,
                    traded_at, outcome_label, source, retrieved_at, request_url, raw_path,
                    content_sha256, limitations_json
                ) VALUES (
                    :trade_id, :transaction_hash, :condition_id, :token_id, :side, :price, :size,
                    :traded_at, :outcome_label, :source, :retrieved_at, :request_url, :raw_path,
                    :content_sha256, :limitations_json
                )
                ON CONFLICT(trade_id) DO UPDATE SET
                    transaction_hash=excluded.transaction_hash,
                    condition_id=excluded.condition_id,
                    token_id=excluded.token_id,
                    side=excluded.side,
                    price=excluded.price,
                    size=excluded.size,
                    traded_at=excluded.traded_at,
                    outcome_label=excluded.outcome_label,
                    source=excluded.source,
                    retrieved_at=excluded.retrieved_at,
                    request_url=excluded.request_url,
                    raw_path=excluded.raw_path,
                    content_sha256=excluded.content_sha256,
                    limitations_json=excluded.limitations_json
                """,
                {
                    "trade_id": trade.trade_id,
                    "transaction_hash": trade.transaction_hash,
                    "condition_id": trade.condition_id,
                    "token_id": trade.token_id,
                    "side": trade.side,
                    "price": trade.price,
                    "size": trade.size,
                    "traded_at": _iso(trade.traded_at),
                    "outcome_label": trade.outcome_label,
                    **cols,
                },
            )

    def upsert_order_book(self, snapshot: OrderBookSnapshot) -> None:
        cols = _prov_cols(snapshot.provenance)
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT snapshot_id FROM order_book_snapshots
                WHERE token_id = ? AND observed_at = ? AND IFNULL(hash, '') = ?
                """,
                (snapshot.token_id, _iso(snapshot.observed_at), snapshot.hash or ""),
            ).fetchone()
            snapshot_id = existing["snapshot_id"] if existing else snapshot.snapshot_id
            conn.execute(
                """
                INSERT INTO order_book_snapshots(
                    snapshot_id, token_id, condition_id, observed_at, hash, tick_size,
                    min_order_size, last_trade_price, source, retrieved_at, request_url,
                    raw_path, content_sha256, limitations_json
                ) VALUES (
                    :snapshot_id, :token_id, :condition_id, :observed_at, :hash, :tick_size,
                    :min_order_size, :last_trade_price, :source, :retrieved_at, :request_url,
                    :raw_path, :content_sha256, :limitations_json
                )
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    token_id=excluded.token_id,
                    condition_id=excluded.condition_id,
                    observed_at=excluded.observed_at,
                    hash=excluded.hash,
                    tick_size=excluded.tick_size,
                    min_order_size=excluded.min_order_size,
                    last_trade_price=excluded.last_trade_price,
                    source=excluded.source,
                    retrieved_at=excluded.retrieved_at,
                    request_url=excluded.request_url,
                    raw_path=excluded.raw_path,
                    content_sha256=excluded.content_sha256,
                    limitations_json=excluded.limitations_json
                """,
                {
                    "snapshot_id": snapshot_id,
                    "token_id": snapshot.token_id,
                    "condition_id": snapshot.condition_id,
                    "observed_at": _iso(snapshot.observed_at),
                    "hash": snapshot.hash,
                    "tick_size": snapshot.tick_size,
                    "min_order_size": snapshot.min_order_size,
                    "last_trade_price": snapshot.last_trade_price,
                    **cols,
                },
            )
            conn.execute("DELETE FROM order_book_levels WHERE snapshot_id = ?", (snapshot_id,))
            conn.executemany(
                """
                INSERT INTO order_book_levels(snapshot_id, side, level_index, price, size)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (snapshot_id, level.side, level.level_index, level.price, level.size)
                    for level in snapshot.levels
                ],
            )

    def upsert_forecast(self, forecast: WeatherForecast) -> None:
        cols = _prov_cols(forecast.provenance)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO weather_forecasts(
                    station_id, provider, model, issued_at, valid_time, variable,
                    temperature_celsius, source_value, source_unit, lead_hours,
                    source, retrieved_at, request_url, raw_path, content_sha256, limitations_json
                ) VALUES (
                    :station_id, :provider, :model, :issued_at, :valid_time, :variable,
                    :temperature_celsius, :source_value, :source_unit, :lead_hours,
                    :source, :retrieved_at, :request_url, :raw_path, :content_sha256, :limitations_json
                )
                ON CONFLICT(
                    station_id, provider, IFNULL(model, ''), IFNULL(issued_at, ''),
                    valid_time, variable
                ) DO UPDATE SET
                    temperature_celsius=excluded.temperature_celsius,
                    source_value=excluded.source_value,
                    source_unit=excluded.source_unit,
                    lead_hours=excluded.lead_hours,
                    source=excluded.source,
                    retrieved_at=excluded.retrieved_at,
                    request_url=excluded.request_url,
                    raw_path=excluded.raw_path,
                    content_sha256=excluded.content_sha256,
                    limitations_json=excluded.limitations_json
                """,
                {
                    "station_id": forecast.station_id,
                    "provider": forecast.provider,
                    "model": forecast.model,
                    "issued_at": _iso(forecast.issued_at),
                    "valid_time": _iso(forecast.valid_time),
                    "variable": forecast.variable,
                    "temperature_celsius": forecast.temperature_celsius,
                    "source_value": forecast.source_value,
                    "source_unit": forecast.source_unit,
                    "lead_hours": forecast.lead_hours,
                    **cols,
                },
            )

    def upsert_ensemble_member(self, member: ForecastEnsembleMember) -> None:
        cols = _prov_cols(member.provenance)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO forecast_ensemble_members(
                    station_id, provider, model, issued_at, valid_time, member_id, variable,
                    temperature_celsius, source_value, source_unit,
                    source, retrieved_at, request_url, raw_path, content_sha256, limitations_json
                ) VALUES (
                    :station_id, :provider, :model, :issued_at, :valid_time, :member_id, :variable,
                    :temperature_celsius, :source_value, :source_unit,
                    :source, :retrieved_at, :request_url, :raw_path, :content_sha256, :limitations_json
                )
                ON CONFLICT(
                    station_id, provider, IFNULL(model, ''), IFNULL(issued_at, ''),
                    valid_time, member_id, variable
                ) DO UPDATE SET
                    temperature_celsius=excluded.temperature_celsius,
                    source_value=excluded.source_value,
                    source_unit=excluded.source_unit,
                    source=excluded.source,
                    retrieved_at=excluded.retrieved_at,
                    request_url=excluded.request_url,
                    raw_path=excluded.raw_path,
                    content_sha256=excluded.content_sha256,
                    limitations_json=excluded.limitations_json
                """,
                {
                    "station_id": member.station_id,
                    "provider": member.provider,
                    "model": member.model,
                    "issued_at": _iso(member.issued_at),
                    "valid_time": _iso(member.valid_time),
                    "member_id": member.member_id,
                    "variable": member.variable,
                    "temperature_celsius": member.temperature_celsius,
                    "source_value": member.source_value,
                    "source_unit": member.source_unit,
                    **cols,
                },
            )

    def upsert_hourly_observation(self, observation: HourlyObservation) -> None:
        cols = _prov_cols(observation.provenance)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO hourly_observations(
                    station_id, provider, observed_at, variable, temperature_celsius,
                    source_value, source_unit, source, retrieved_at, request_url, raw_path,
                    content_sha256, limitations_json
                ) VALUES (
                    :station_id, :provider, :observed_at, :variable, :temperature_celsius,
                    :source_value, :source_unit, :source, :retrieved_at, :request_url, :raw_path,
                    :content_sha256, :limitations_json
                )
                ON CONFLICT(station_id, provider, observed_at, variable) DO UPDATE SET
                    temperature_celsius=excluded.temperature_celsius,
                    source_value=excluded.source_value,
                    source_unit=excluded.source_unit,
                    source=excluded.source,
                    retrieved_at=excluded.retrieved_at,
                    request_url=excluded.request_url,
                    raw_path=excluded.raw_path,
                    content_sha256=excluded.content_sha256,
                    limitations_json=excluded.limitations_json
                """,
                {
                    "station_id": observation.station_id,
                    "provider": observation.provider,
                    "observed_at": _iso(observation.observed_at),
                    "variable": observation.variable,
                    "temperature_celsius": observation.temperature_celsius,
                    "source_value": observation.source_value,
                    "source_unit": observation.source_unit,
                    **cols,
                },
            )

    def upsert_daily_maximum(self, daily: DailyActualMaximum) -> None:
        cols = _prov_cols(daily.provenance)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_actual_maxima(
                    station_id, provider, local_date, timezone_name, temperature_celsius,
                    source_value, source_unit, source, retrieved_at, request_url, raw_path,
                    content_sha256, limitations_json
                ) VALUES (
                    :station_id, :provider, :local_date, :timezone_name, :temperature_celsius,
                    :source_value, :source_unit, :source, :retrieved_at, :request_url, :raw_path,
                    :content_sha256, :limitations_json
                )
                ON CONFLICT(station_id, provider, local_date) DO UPDATE SET
                    timezone_name=excluded.timezone_name,
                    temperature_celsius=excluded.temperature_celsius,
                    source_value=excluded.source_value,
                    source_unit=excluded.source_unit,
                    source=excluded.source,
                    retrieved_at=excluded.retrieved_at,
                    request_url=excluded.request_url,
                    raw_path=excluded.raw_path,
                    content_sha256=excluded.content_sha256,
                    limitations_json=excluded.limitations_json
                """,
                {
                    "station_id": daily.station_id,
                    "provider": daily.provider,
                    "local_date": daily.local_date,
                    "timezone_name": daily.timezone_name,
                    "temperature_celsius": daily.temperature_celsius,
                    "source_value": daily.source_value,
                    "source_unit": daily.source_unit,
                    **cols,
                },
            )

    def record_raw_payload(
        self,
        *,
        content_sha256: str,
        source: str,
        request_url: str,
        retrieved_at: datetime,
        disk_path: str,
        limitations: tuple[str, ...] = (),
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO raw_payloads(
                    content_sha256, source, request_url, retrieved_at, disk_path, limitations_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_sha256, request_url) DO UPDATE SET
                    source=excluded.source,
                    request_url=excluded.request_url,
                    retrieved_at=excluded.retrieved_at,
                    disk_path=excluded.disk_path,
                    limitations_json=excluded.limitations_json
                """,
                (
                    content_sha256,
                    source,
                    request_url,
                    _iso(retrieved_at),
                    disk_path,
                    _json(limitations),
                ),
            )

    def iter_markets(self) -> Iterator[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM markets ORDER BY condition_id").fetchall()
        yield from rows
