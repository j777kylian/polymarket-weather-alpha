"""Metrics, executable-edge semantics, and non-executable backtest."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from weather_alpha.research.backtest import (
    EDGE_THRESHOLDS,
    DescriptiveBacktester,
    classify_bucket_region,
)
from weather_alpha.research.metrics import (
    clipped_log_loss,
    expected_calibration_error,
    multiclass_brier,
)
from weather_alpha.research.mispricing import raw_edge
from weather_alpha.research.types import ResearchSnapshot


def _snap(
    *,
    token: str,
    p_market: float | None,
    ask: float | None = None,
    bucket: str = "31°C",
    kind: str = "exact",
    city: str = "paris",
    event_date: str = "2026-07-15",
) -> ResearchSnapshot:
    return ResearchSnapshot(
        condition_id="c",
        market_id="m",
        token_id=token,
        city=city,
        station_icao="LFPG",
        event_date=event_date,
        bucket_label=bucket,
        bucket_kind=kind,
        temperature_celsius_min=31.0 if kind == "exact" else None,
        temperature_celsius_max=31.0 if kind == "exact" else (30.0 if kind == "below" else None),
        decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        market_probability=p_market,
        executable_entry_price=ask,
        best_bid=None,
        best_ask=ask,
        midpoint=None,
        spread=None,
        volume=None,
        liquidity=None,
        weather_issued_at=datetime(2026, 7, 14, 0, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 14, 6, 0, tzinfo=UTC),
        forecast_daily_max_c=30.0,
        observation_max_so_far_c=None,
        observation_as_of=None,
        settlement_label="Yes",
        diagnostic_actual_max_c=31.0,
        provenance_urls=(),
        raw_paths=(),
        content_hashes=(),
        limitations=("price-history p is descriptive only",),
    )


def test_raw_edge_is_model_minus_descriptive_market_probability() -> None:
    assert raw_edge(model_probability=0.40, market_probability=0.25) == pytest.approx(0.15)
    assert raw_edge(model_probability=0.10, market_probability=None) is None


def test_executable_edge_requires_ask_and_stays_null_from_price_history() -> None:
    snap = _snap(token="yes", p_market=0.25, ask=None)
    assert snap.best_ask is None
    assert snap.executable_entry_price is None
    result = DescriptiveBacktester().evaluate(
        snapshots=(snap,),
        model_probabilities={"yes": 0.40},
        thresholds=EDGE_THRESHOLDS,
        split_name="validation",
    )
    assert result.status in {"insufficient_data", "non_executable"}
    assert result.executable_trades == 0
    assert result.pnl is None
    assert result.roi is None
    assert result.max_drawdown is None
    assert result.profit_factor is None
    assert result.candidates >= 1


def test_backtest_does_not_fabricate_zero_pnl_when_asks_missing() -> None:
    snaps = (
        _snap(token="a", p_market=0.10),
        _snap(token="b", p_market=0.20, event_date="2026-07-16"),
    )
    result = DescriptiveBacktester().evaluate(
        snapshots=snaps,
        model_probabilities={"a": 0.40, "b": 0.50},
        thresholds=EDGE_THRESHOLDS,
        split_name="test",
    )
    assert result.executable_trades == 0
    assert result.pnl is not None or result.pnl is None
    assert result.pnl is None
    assert result.fees_mode == "not_applied_non_executable"
    assert result.selected_threshold is None
    assert result.threshold_selection_reason is not None
    assert "ask" in result.threshold_selection_reason.lower()
    assert (
        "not occur" in result.threshold_selection_reason.lower()
        or "did not" in result.threshold_selection_reason.lower()
    )
    assert result.candidates_by_threshold
    assert list(result.candidates_by_threshold) == list(EDGE_THRESHOLDS)
    assert result.candidates_by_threshold[0.05] >= 1


def test_probability_metrics_remain_reportable_without_fills() -> None:
    brier = multiclass_brier(
        predicted=((0.7, 0.3), (0.2, 0.8)),
        outcomes=(0, 1),
    )
    ll = clipped_log_loss(
        predicted=((0.7, 0.3), (0.2, 0.8)),
        outcomes=(0, 1),
    )
    ece = expected_calibration_error(
        probabilities=(0.7, 0.2),
        outcomes=(1, 0),
        n_bins=2,
    )
    assert 0 <= brier <= 2
    assert ll > 0
    assert 0 <= ece <= 1


def test_center_vs_tail_bucket_classification() -> None:
    assert classify_bucket_region("exact") == "center"
    assert classify_bucket_region("range") == "center"
    assert classify_bucket_region("below") == "tail"
    assert classify_bucket_region("above") == "tail"
