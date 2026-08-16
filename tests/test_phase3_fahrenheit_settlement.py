"""Blocker 1: Fahrenheit settlement matching uses native-unit rounding, no gaps."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from weather_alpha.models.units import celsius_to_fahrenheit, fahrenheit_to_celsius
from weather_alpha.research.model import (
    ForecastErrorBucketModel,
    _matching_bucket_index,
    match_settlement_bucket,
    round_to_settlement_degree,
)
from weather_alpha.research.types import ResearchSnapshot


def _snap(
    *,
    label: str,
    kind: str,
    lo_f: float | None,
    hi_f: float | None,
    token: str | None = None,
    event_date: str = "2026-03-21",
    forecast: float | None = 12.0,
    diagnostic: float | None = 12.0,
    settlement: str = "No",
) -> ResearchSnapshot:
    lo = None if lo_f is None else fahrenheit_to_celsius(lo_f)
    hi = None if hi_f is None else fahrenheit_to_celsius(hi_f)
    return ResearchSnapshot(
        condition_id=label,
        market_id="m",
        token_id=token or label,
        city="new york",
        station_icao="KLGA",
        event_date=event_date,
        bucket_label=label,
        bucket_kind=kind,
        temperature_celsius_min=lo,
        temperature_celsius_max=hi,
        decision_ts=datetime(2026, 3, 20, 12, 0, tzinfo=UTC),
        market_probability=0.1,
        executable_entry_price=None,
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        volume=None,
        liquidity=None,
        weather_issued_at=None,
        weather_available_at=None,
        forecast_daily_max_c=forecast,
        observation_max_so_far_c=None,
        observation_as_of=None,
        settlement_label=settlement,
        diagnostic_actual_max_c=diagnostic,
        provenance_urls=(),
        raw_paths=(),
        content_hashes=(),
        limitations=(),
        temperature_unit="F",
        temperature_native_min=lo_f,
        temperature_native_max=hi_f,
    )


NY_FAMILY = (
    _snap(label="41°F or below", kind="below", lo_f=None, hi_f=41),
    _snap(label="42-43°F", kind="range", lo_f=42, hi_f=43),
    _snap(label="44-45°F", kind="range", lo_f=44, hi_f=45),
    _snap(label="46-47°F", kind="range", lo_f=46, hi_f=47),
    _snap(label="48-49°F", kind="range", lo_f=48, hi_f=49),
    _snap(label="50-51°F", kind="range", lo_f=50, hi_f=51),
    _snap(label="52-53°F", kind="range", lo_f=52, hi_f=53),
    _snap(label="54-55°F", kind="range", lo_f=54, hi_f=55),
    _snap(label="56-57°F", kind="range", lo_f=56, hi_f=57),
    _snap(label="58-59°F", kind="range", lo_f=58, hi_f=59),
    _snap(label="60°F or higher", kind="above", lo_f=60, hi_f=None),
)


def test_round_to_settlement_degree_boundaries() -> None:
    assert round_to_settlement_degree(53.4) == 53
    assert round_to_settlement_degree(53.5) == 54
    assert round_to_settlement_degree(53.6) == 54
    assert round_to_settlement_degree(41.0) == 41
    assert round_to_settlement_degree(40.9) == 41
    assert round_to_settlement_degree(40.4) == 40


def test_53_6f_and_neighbors_match_expected_integer_buckets() -> None:
    # Continuous values that previously fell into Celsius conversion gaps.
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(53.6)) == "54-55°F"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(53.4)) == "52-53°F"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(53.0)) == "52-53°F"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(54.0)) == "54-55°F"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(53.5)) == "54-55°F"


def test_just_below_on_and_above_integer_fahrenheit_thresholds() -> None:
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(41.4)) == "41°F or below"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(41.5)) == "42-43°F"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(41.6)) == "42-43°F"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(59.4)) == "58-59°F"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(59.5)) == "60°F or higher"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(59.6)) == "60°F or higher"


def test_adjacent_fahrenheit_buckets_cover_domain_without_gaps() -> None:
    unmatched: list[float] = []
    for tenths in range(350, 701):
        value_f = tenths / 10.0
        value_c = fahrenheit_to_celsius(value_f)
        if _matching_bucket_index(NY_FAMILY, value_c) is None:
            unmatched.append(value_f)
    assert unmatched == []


def test_open_ended_fahrenheit_tails() -> None:
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(10.0)) == "41°F or below"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(41.0)) == "41°F or below"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(60.0)) == "60°F or higher"
    assert match_settlement_bucket(NY_FAMILY, fahrenheit_to_celsius(99.0)) == "60°F or higher"


def test_celsius_markets_remain_unchanged() -> None:
    family = (
        ResearchSnapshot(
            condition_id="30",
            market_id="m",
            token_id="a",
            city="paris",
            station_icao="LFPG",
            event_date="2026-07-15",
            bucket_label="30°C",
            bucket_kind="exact",
            temperature_celsius_min=30.0,
            temperature_celsius_max=30.0,
            decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            market_probability=0.2,
            executable_entry_price=None,
            best_bid=None,
            best_ask=None,
            midpoint=None,
            spread=None,
            volume=None,
            liquidity=None,
            weather_issued_at=None,
            weather_available_at=None,
            forecast_daily_max_c=30.0,
            observation_max_so_far_c=None,
            observation_as_of=None,
            settlement_label="No",
            diagnostic_actual_max_c=30.0,
            provenance_urls=(),
            raw_paths=(),
            content_hashes=(),
            limitations=(),
            temperature_unit="C",
            temperature_native_min=30.0,
            temperature_native_max=30.0,
        ),
        ResearchSnapshot(
            condition_id="31",
            market_id="m",
            token_id="b",
            city="paris",
            station_icao="LFPG",
            event_date="2026-07-15",
            bucket_label="31°C",
            bucket_kind="exact",
            temperature_celsius_min=31.0,
            temperature_celsius_max=31.0,
            decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            market_probability=0.2,
            executable_entry_price=None,
            best_bid=None,
            best_ask=None,
            midpoint=None,
            spread=None,
            volume=None,
            liquidity=None,
            weather_issued_at=None,
            weather_available_at=None,
            forecast_daily_max_c=30.0,
            observation_max_so_far_c=None,
            observation_as_of=None,
            settlement_label="Yes",
            diagnostic_actual_max_c=31.0,
            provenance_urls=(),
            raw_paths=(),
            content_hashes=(),
            limitations=(),
            temperature_unit="C",
            temperature_native_min=31.0,
            temperature_native_max=31.0,
        ),
    )
    assert match_settlement_bucket(family, 30.0) == "30°C"
    assert match_settlement_bucket(family, 31.0) == "31°C"
    assert match_settlement_bucket(family, 30.4) == "30°C"
    assert match_settlement_bucket(family, 30.6) == "31°C"


def test_forecast_error_model_does_not_discard_fahrenheit_gap_samples() -> None:
    forecast = fahrenheit_to_celsius(50.0)
    diagnostic = fahrenheit_to_celsius(53.6)
    train_event = (
        _snap(
            label="52-53°F",
            kind="range",
            lo_f=52,
            hi_f=53,
            token="tr-a",
            event_date="2026-03-01",
            forecast=forecast,
            diagnostic=diagnostic,
            settlement="Yes",
        ),
        _snap(
            label="54-55°F",
            kind="range",
            lo_f=54,
            hi_f=55,
            token="tr-b",
            event_date="2026-03-01",
            forecast=forecast,
            diagnostic=diagnostic,
            settlement="No",
        ),
    )
    model = ForecastErrorBucketModel()
    model.fit(train_event)
    event = (
        replace(train_event[0], token_id="ev-a", event_date="2026-03-21", settlement_label="No"),
        replace(train_event[1], token_id="ev-b", event_date="2026-03-21", settlement_label="No"),
    )
    probs = model.predict_event(event)
    by_label = {row.label: row.probability for row in probs}
    assert by_label["54-55°F"] == pytest.approx(1.0)
    assert by_label["52-53°F"] == pytest.approx(0.0)
    assert celsius_to_fahrenheit(fahrenheit_to_celsius(53.6)) == pytest.approx(53.6)
