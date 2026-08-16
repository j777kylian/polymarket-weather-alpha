"""Phase 3 probability models: train-only fit, normalization, no settlement features."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from weather_alpha.research.model import (
    ForecastErrorBucketModel,
    HistoricalFrequencyBaseline,
    ModelFeatures,
    probabilities_sum_to_one,
)
from weather_alpha.research.split import SplitDates
from weather_alpha.research.types import ResearchSnapshot


def _snap(
    *,
    event_date: str,
    station: str = "LFPG",
    bucket: str = "31°C",
    kind: str = "exact",
    min_c: float | None = 31.0,
    max_c: float | None = 31.0,
    forecast: float | None = 30.0,
    settlement: str | None = "No",
    city: str = "paris",
    token: str = "t",
) -> ResearchSnapshot:
    return ResearchSnapshot(
        condition_id=f"{station}-{event_date}-{bucket}",
        market_id="m",
        token_id=token,
        city=city,
        station_icao=station,
        event_date=event_date,
        bucket_label=bucket,
        bucket_kind=kind,
        temperature_celsius_min=min_c,
        temperature_celsius_max=max_c,
        decision_ts=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        market_probability=0.2,
        executable_entry_price=None,
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        volume=None,
        liquidity=None,
        weather_issued_at=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 1, 6, 0, tzinfo=UTC),
        forecast_daily_max_c=forecast,
        observation_max_so_far_c=None,
        observation_as_of=None,
        settlement_label=settlement,
        diagnostic_actual_max_c=31.0 if settlement == "Yes" else 28.0,
        provenance_urls=(),
        raw_paths=(),
        content_hashes=(),
        limitations=("test",),
    )


def test_frequency_baseline_uses_train_labels_only() -> None:
    train = (
        _snap(
            event_date="2026-07-01", bucket="30°C", min_c=30, max_c=30, settlement="Yes", token="a"
        ),
        _snap(
            event_date="2026-07-01", bucket="31°C", min_c=31, max_c=31, settlement="No", token="b"
        ),
        _snap(
            event_date="2026-07-02", bucket="30°C", min_c=30, max_c=30, settlement="No", token="c"
        ),
        _snap(
            event_date="2026-07-02", bucket="31°C", min_c=31, max_c=31, settlement="Yes", token="d"
        ),
    )
    test = (
        _snap(
            event_date="2026-07-10", bucket="30°C", min_c=30, max_c=30, settlement="Yes", token="e"
        ),
        _snap(
            event_date="2026-07-10", bucket="31°C", min_c=31, max_c=31, settlement="No", token="f"
        ),
    )
    model = HistoricalFrequencyBaseline()
    model.fit(train)
    # Train-only: 30°C and 31°C each won once of two event-days? Per-bucket YES rate:
    # 30°C YES on 1 of 2 labeled rows for that bucket => 0.5
    probs = model.predict_event(test)
    assert probabilities_sum_to_one(tuple(p.probability for p in probs))
    by_label = {p.label: p.probability for p in probs}
    assert by_label["30°C"] == pytest.approx(0.5)
    assert by_label["31°C"] == pytest.approx(0.5)


def test_forecast_error_model_does_not_use_validation_or_test_labels() -> None:
    train = (
        _snap(
            event_date="2026-07-01",
            bucket="30°C",
            min_c=30,
            max_c=30,
            forecast=30.0,
            settlement="Yes",
            token="a",
        ),
        _snap(
            event_date="2026-07-01",
            bucket="31°C",
            min_c=31,
            max_c=31,
            forecast=30.0,
            settlement="No",
            token="b",
        ),
        _snap(
            event_date="2026-07-02",
            bucket="30°C",
            min_c=30,
            max_c=30,
            forecast=29.0,
            settlement="No",
            token="c",
        ),
        _snap(
            event_date="2026-07-02",
            bucket="31°C",
            min_c=31,
            max_c=31,
            forecast=29.0,
            settlement="Yes",
            token="d",
        ),
    )
    model = ForecastErrorBucketModel()
    split = SplitDates(
        train=("2026-07-01", "2026-07-02"), validation=("2026-07-08",), test=("2026-07-10",)
    )
    model.fit(train, split=split)
    # Validation labels are not passed into fit; 40°C should not dominate at forecast=30.
    event = (
        _snap(
            event_date="2026-07-10", bucket="30°C", min_c=30, max_c=30, forecast=30.0, token="t1"
        ),
        _snap(
            event_date="2026-07-10", bucket="31°C", min_c=31, max_c=31, forecast=30.0, token="t2"
        ),
        _snap(
            event_date="2026-07-10", bucket="40°C", min_c=40, max_c=40, forecast=30.0, token="t3"
        ),
    )
    probs = model.predict_event(event)
    assert probabilities_sum_to_one(tuple(p.probability for p in probs))
    by_label = {p.label: p.probability for p in probs}
    assert by_label["40°C"] < 0.5


def test_settlement_label_is_not_a_model_feature() -> None:
    features = ModelFeatures.from_snapshot(
        _snap(event_date="2026-07-01", settlement="Yes", forecast=28.0)
    )
    dumped = features.as_dict()
    assert "settlement_label" not in dumped
    assert "diagnostic_actual_max_c" not in dumped
    assert features.forecast_daily_max_c == 28.0


def test_probability_normalization_rejects_non_unit_sums() -> None:
    assert probabilities_sum_to_one((0.2, 0.3, 0.5))
    assert not probabilities_sum_to_one((0.2, 0.3))
    assert not probabilities_sum_to_one((0.0, 0.0, 0.0))
