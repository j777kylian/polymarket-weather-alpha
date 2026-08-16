"""Tail-price analysis: descriptive only; executable survival unknown without asks."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from weather_alpha.research.tail import analyze_tails, classify_tail_band
from weather_alpha.research.types import ResearchSnapshot


def _snap(*, token: str, p: float, model_p: float, settlement: str) -> ResearchSnapshot:
    return ResearchSnapshot(
        condition_id="c",
        market_id="m",
        token_id=token,
        city="paris",
        station_icao="LFPG",
        event_date="2026-07-15",
        bucket_label="40°C or higher",
        bucket_kind="above",
        temperature_celsius_min=40.0,
        temperature_celsius_max=None,
        decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        market_probability=p,
        executable_entry_price=None,
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        volume=None,
        liquidity=None,
        weather_issued_at=datetime(2026, 7, 14, 0, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 14, 6, 0, tzinfo=UTC),
        forecast_daily_max_c=31.0,
        observation_max_so_far_c=None,
        observation_as_of=None,
        settlement_label=settlement,
        diagnostic_actual_max_c=31.0,
        provenance_urls=(),
        raw_paths=(),
        content_hashes=(),
        limitations=("price-history p is descriptive only",),
    )


def test_tail_band_classification() -> None:
    assert classify_tail_band(0.005) == "<1c"
    assert classify_tail_band(0.02) == "1-3c"
    assert classify_tail_band(0.04) == "3-5c"
    assert classify_tail_band(0.10) is None
    assert classify_tail_band(None) is None


def test_tail_analysis_marks_executable_survival_unknown_and_null_pnl() -> None:
    rows = (
        _snap(token="a", p=0.005, model_p=0.02, settlement="No"),
        _snap(token="b", p=0.02, model_p=0.03, settlement="Yes"),
        _snap(token="c", p=0.04, model_p=0.01, settlement="No"),
    )
    report = analyze_tails(
        snapshots=rows,
        model_probabilities={"a": 0.02, "b": 0.03, "c": 0.01},
        split_name="test",
    )
    assert report.executable_survival is None
    assert report.executable_survival_status == "unknown_no_historical_asks"
    assert report.pnl is None
    assert report.roi is None
    assert report.max_drawdown is None
    assert report.profit_factor is None
    assert report.robustness_remove_largest_1_pnl is None
    assert report.robustness_remove_largest_3_pnl is None
    assert report.robustness_remove_largest_5_pnl is None
    bands = {row.band: row for row in report.bands}
    assert bands["<1c"].n == 1
    assert bands["1-3c"].settled_yes_fraction == 1.0
    assert bands["3-5c"].settled_yes_fraction == 0.0
    assert bands["<1c"].settled_yes_count == 0
    assert bands["1-3c"].settled_yes_count == 1
    assert bands["3-5c"].settled_yes_count == 0
    assert report.jackpot_concentration is None
    assert report.max_band_settled_yes_share == pytest.approx(1.0)
    assert (
        "not return" in " ".join(report.notes).lower()
        or "not a return" in " ".join(report.notes).lower()
    )
