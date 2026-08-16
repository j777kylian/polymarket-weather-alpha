"""No-lookahead snapshot assembly and duplicate/quarantine handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from weather_alpha.research.dataset import (
    LookaheadError,
    assemble_snapshot,
    dedupe_snapshots,
    read_snapshots_parquet,
    row_to_snapshot,
    snapshot_to_row,
    write_snapshots_parquet,
)
from weather_alpha.research.prices import PricePoint
from weather_alpha.research.types import HourlyForecastState, QuarantineRecord, ResearchSnapshot


def test_assemble_snapshot_rejects_weather_before_available_at() -> None:
    decision = datetime(2026, 7, 14, 5, 0, tzinfo=UTC)
    issued = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    available = issued + timedelta(hours=6)
    with pytest.raises(LookaheadError):
        assemble_snapshot(
            **_base_kwargs(decision_ts=decision),
            weather_issued_at=issued,
            weather_available_at=available,
            forecast_daily_max_c=30.0,
            price=PricePoint(observed_at=datetime(2026, 7, 14, 4, 0, tzinfo=UTC), price=0.4),
        )


def test_assemble_snapshot_rejects_future_price() -> None:
    decision = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    with pytest.raises(LookaheadError):
        assemble_snapshot(
            **_base_kwargs(decision_ts=decision),
            weather_issued_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
            weather_available_at=datetime(2026, 7, 13, 18, 0, tzinfo=UTC),
            forecast_daily_max_c=30.0,
            price=PricePoint(observed_at=datetime(2026, 7, 14, 13, 0, tzinfo=UTC), price=0.9),
        )


def test_observation_after_decision_is_not_used() -> None:
    decision = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    with pytest.raises(LookaheadError):
        assemble_snapshot(
            **_base_kwargs(decision_ts=decision),
            weather_issued_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
            weather_available_at=datetime(2026, 7, 13, 18, 0, tzinfo=UTC),
            forecast_daily_max_c=30.0,
            price=PricePoint(observed_at=datetime(2026, 7, 14, 11, 0, tzinfo=UTC), price=0.4),
            observation_max_so_far_c=25.0,
            observation_as_of=datetime(2026, 7, 14, 13, 0, tzinfo=UTC),
        )


def test_missing_price_stays_null_not_zero() -> None:
    snap = assemble_snapshot(
        **_base_kwargs(decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC)),
        weather_issued_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 13, 18, 0, tzinfo=UTC),
        forecast_daily_max_c=30.0,
        price=None,
    )
    assert snap.market_probability is None
    assert snap.executable_entry_price is None
    assert snap.best_ask is None


def test_executable_fields_remain_null_from_price_history() -> None:
    snap = assemble_snapshot(
        **_base_kwargs(decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC)),
        weather_issued_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 13, 18, 0, tzinfo=UTC),
        forecast_daily_max_c=30.0,
        price=PricePoint(observed_at=datetime(2026, 7, 14, 11, 0, tzinfo=UTC), price=0.42),
    )
    assert snap.market_probability == 0.42
    assert snap.executable_entry_price is None
    assert snap.best_bid is None
    assert snap.best_ask is None
    assert snap.midpoint is None
    assert snap.spread is None
    assert any(
        "descriptive" in item.lower() or "prices-history" in item.lower()
        for item in snap.limitations
    )


def test_dedupe_keeps_first_and_records_duplicate() -> None:
    ts = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    a = assemble_snapshot(
        **_base_kwargs(decision_ts=ts, token_id="yes"),
        weather_issued_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 13, 18, 0, tzinfo=UTC),
        forecast_daily_max_c=30.0,
        price=PricePoint(observed_at=datetime(2026, 7, 14, 11, 0, tzinfo=UTC), price=0.4),
    )
    b = assemble_snapshot(
        **_base_kwargs(decision_ts=ts, token_id="yes"),
        weather_issued_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 13, 18, 0, tzinfo=UTC),
        forecast_daily_max_c=31.0,
        price=PricePoint(observed_at=datetime(2026, 7, 14, 11, 0, tzinfo=UTC), price=0.5),
    )
    unique, quarantined = dedupe_snapshots((a, b))
    assert len(unique) == 1
    assert unique[0].forecast_daily_max_c == 30.0
    assert any(
        isinstance(row, QuarantineRecord) and "duplicate" in row.reason for row in quarantined
    )


def _base_kwargs(*, decision_ts: datetime, token_id: str = "token-yes") -> dict[str, Any]:
    return {
        "condition_id": "cond-1",
        "market_id": "m-1",
        "token_id": token_id,
        "city": "paris",
        "station_icao": "LFPG",
        "event_date": "2026-07-15",
        "bucket_label": "31°C",
        "bucket_kind": "exact",
        "temperature_celsius_min": 31.0,
        "temperature_celsius_max": 31.0,
        "decision_ts": decision_ts,
        "settlement_label": "Yes",
        "diagnostic_actual_max_c": 31.2,
        "provenance_urls": ("https://example.test",),
        "raw_paths": ("/tmp/a.json",),
        "content_hashes": ("abc",),
    }


def test_price_observed_at_is_stored_separately_and_rejected_after_decision() -> None:
    decision = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    observed = datetime(2026, 7, 14, 11, 0, tzinfo=UTC)
    snap = assemble_snapshot(
        **_base_kwargs(decision_ts=decision),
        weather_issued_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 13, 18, 0, tzinfo=UTC),
        forecast_daily_max_c=30.0,
        price=PricePoint(observed_at=observed, price=0.42),
        price_request_url="https://clob.polymarket.com/prices-history?market=token-yes",
        price_raw_path="/tmp/price.json",
        price_content_sha256="pricehash",
    )
    assert snap.market_probability == 0.42
    assert snap.market_price_observed_at == observed
    assert snap.market_price_observed_at != snap.decision_ts
    assert snap.price_request_url is not None
    assert "prices-history" in snap.price_request_url
    assert snap.price_raw_path == "/tmp/price.json"
    assert snap.price_content_sha256 == "pricehash"
    with pytest.raises(LookaheadError):
        assemble_snapshot(
            **_base_kwargs(decision_ts=decision),
            weather_issued_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
            weather_available_at=datetime(2026, 7, 13, 18, 0, tzinfo=UTC),
            forecast_daily_max_c=30.0,
            price=PricePoint(observed_at=datetime(2026, 7, 14, 13, 0, tzinfo=UTC), price=0.9),
        )


def test_forecast_hourly_and_price_fields_jsonl_parquet_roundtrip(tmp_path: Path) -> None:

    hourly = (
        HourlyForecastState(
            valid_time_utc=datetime(2026, 7, 15, 0, 0, tzinfo=UTC),
            temperature_c=22.0,
            dew_point_c=12.0,
            humidity_pct=50.0,
            cloud_cover_pct=10.0,
            wind_speed=8.0,
            wind_direction_deg=180.0,
            precipitation=0.0,
            surface_pressure=1012.0,
        ),
        HourlyForecastState(
            valid_time_utc=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
            temperature_c=31.0,
            dew_point_c=13.0,
            humidity_pct=35.0,
            cloud_cover_pct=30.0,
            wind_speed=12.0,
            wind_direction_deg=200.0,
            precipitation=0.1,
            surface_pressure=1010.0,
        ),
    )
    snap = assemble_snapshot(
        **_base_kwargs(decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC)),
        weather_issued_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 13, 18, 0, tzinfo=UTC),
        forecast_daily_max_c=31.0,
        price=PricePoint(observed_at=datetime(2026, 7, 14, 11, 0, tzinfo=UTC), price=0.4),
        price_request_url="https://clob.polymarket.com/prices-history?market=token-yes",
        price_raw_path="/tmp/price.json",
        price_content_sha256="pricehash",
        weather_valid_times=(datetime(2026, 7, 15, 12, 0, tzinfo=UTC),),
        forecast_hourly=hourly,
    )
    row = snapshot_to_row(snap)
    assert snap.market_price_observed_at is not None
    assert row["market_price_observed_at"] == snap.market_price_observed_at.isoformat()
    restored = row_to_snapshot(row)
    assert restored.forecast_hourly == snap.forecast_hourly
    assert restored.weather_valid_times == snap.weather_valid_times
    assert restored.market_price_observed_at == snap.market_price_observed_at
    assert restored.price_content_sha256 == "pricehash"
    parquet_path = Path(tmp_path) / "snaps.parquet"
    write_snapshots_parquet(parquet_path, (snap,))
    from_parquet = row_to_snapshot(read_snapshots_parquet(parquet_path)[0])
    assert from_parquet.forecast_hourly == snap.forecast_hourly
    assert from_parquet.price_request_url == snap.price_request_url


def test_research_snapshot_type_is_immutable() -> None:
    snap = assemble_snapshot(
        **_base_kwargs(decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC)),
        weather_issued_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 13, 18, 0, tzinfo=UTC),
        forecast_daily_max_c=30.0,
        price=PricePoint(observed_at=datetime(2026, 7, 14, 11, 0, tzinfo=UTC), price=0.4),
    )
    assert isinstance(snap, ResearchSnapshot)
    with pytest.raises(AttributeError):
        snap.market_probability = 0.0  # type: ignore[misc]
