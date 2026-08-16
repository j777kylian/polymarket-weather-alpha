"""Blocker 5: executable survival uses true/false/null without contradictions."""

from __future__ import annotations

from datetime import UTC, datetime

from weather_alpha.research.tail import analyze_tails
from weather_alpha.research.types import ResearchSnapshot


def _snap(
    *,
    token: str,
    p: float,
    settlement: str,
    best_ask: float | None = None,
    executable_entry_price: float | None = None,
) -> ResearchSnapshot:
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
        executable_entry_price=executable_entry_price,
        best_bid=None,
        best_ask=best_ask,
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


def test_executable_data_unavailable_uses_null_not_false() -> None:
    rows = (
        _snap(token="a", p=0.005, settlement="No"),
        _snap(token="b", p=0.02, settlement="Yes"),
        _snap(token="c", p=0.04, settlement="No"),
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
    # No contradictory false+unknown encoding.
    assert report.executable_survival is None
    assert report.executable_survival_status.startswith("unknown")


def test_executable_survival_true_when_measured_and_survived() -> None:
    rows = (
        _snap(
            token="a",
            p=0.005,
            settlement="Yes",
            best_ask=0.01,
            executable_entry_price=0.01,
        ),
    )
    report = analyze_tails(
        snapshots=rows,
        model_probabilities={"a": 0.02},
        split_name="test",
        executable_survival=True,
        executable_survival_status="measured_survived",
    )
    assert report.executable_survival is True
    assert report.executable_survival_status == "measured_survived"


def test_executable_survival_false_when_measured_and_failed() -> None:
    rows = (
        _snap(
            token="a",
            p=0.005,
            settlement="No",
            best_ask=0.01,
            executable_entry_price=0.01,
        ),
    )
    report = analyze_tails(
        snapshots=rows,
        model_probabilities={"a": 0.02},
        split_name="test",
        executable_survival=False,
        executable_survival_status="measured_did_not_survive",
    )
    assert report.executable_survival is False
    assert report.executable_survival_status == "measured_did_not_survive"
