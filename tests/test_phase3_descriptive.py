"""Descriptive mispricing breakdowns for Phase 3D backtest reporting."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from weather_alpha.research.backtest import (
    EDGE_THRESHOLDS,
    DescriptiveBacktester,
    classify_entry_price_bucket,
    classify_lead_time_bucket,
    classify_raw_edge_bucket,
)
from weather_alpha.research.reports import write_report_pair
from weather_alpha.research.run import run_phase3
from weather_alpha.research.types import ResearchSnapshot


def _snap(
    *,
    token: str,
    p_market: float | None,
    lead_hours: float | None = 24.0,
    ask: float | None = None,
    bucket: str = "31°C",
    kind: str | None = "exact",
    city: str | None = "paris",
    station: str | None = "LFPG",
    event_date: str = "2026-07-15",
    settlement: str | None = "Yes",
) -> ResearchSnapshot:
    return ResearchSnapshot(
        condition_id=f"c-{token}",
        market_id="m",
        token_id=token,
        city=city,
        station_icao=station,
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
        settlement_label=settlement,
        diagnostic_actual_max_c=31.0,
        provenance_urls=(),
        raw_paths=(),
        content_hashes=(),
        limitations=("price-history p is descriptive only",),
        forecast_lead_hours=lead_hours,
    )


def test_raw_edge_bucket_boundaries() -> None:
    assert classify_raw_edge_bucket(0.0) == "<=0%"
    assert classify_raw_edge_bucket(-0.01) == "<=0%"
    assert classify_raw_edge_bucket(0.0001) == "0-5%"
    assert classify_raw_edge_bucket(0.05) == "0-5%"
    assert classify_raw_edge_bucket(0.0500001) == "5-10%"
    assert classify_raw_edge_bucket(0.10) == "5-10%"
    assert classify_raw_edge_bucket(0.1000001) == "10-15%"
    assert classify_raw_edge_bucket(0.15) == "10-15%"
    assert classify_raw_edge_bucket(0.1500001) == "15-20%"
    assert classify_raw_edge_bucket(0.20) == "15-20%"
    assert classify_raw_edge_bucket(0.2000001) == ">20%"
    assert classify_raw_edge_bucket(0.50) == ">20%"


def test_entry_price_bucket_boundaries() -> None:
    assert classify_entry_price_bucket(0.0) == "<1c"
    assert classify_entry_price_bucket(0.009999) == "<1c"
    assert classify_entry_price_bucket(0.01) == "1-3c"
    assert classify_entry_price_bucket(0.029999) == "1-3c"
    assert classify_entry_price_bucket(0.03) == "3-5c"
    assert classify_entry_price_bucket(0.049999) == "3-5c"
    assert classify_entry_price_bucket(0.05) == "5-10c"
    assert classify_entry_price_bucket(0.099999) == "5-10c"
    assert classify_entry_price_bucket(0.10) == "10-25c"
    assert classify_entry_price_bucket(0.25) == "10-25c"
    assert classify_entry_price_bucket(0.2500001) == ">25c"
    assert classify_entry_price_bucket(0.99) == ">25c"
    assert classify_entry_price_bucket(None) is None


def test_lead_time_bucket_boundaries_exact_1h_is_1_to_6h() -> None:
    assert classify_lead_time_bucket(1.0) == "1-6h"
    assert classify_lead_time_bucket(0.999) == "<1h"
    assert classify_lead_time_bucket(6.0) == "1-6h"
    assert classify_lead_time_bucket(6.0001) == "6-12h"
    assert classify_lead_time_bucket(12.0) == "6-12h"
    assert classify_lead_time_bucket(12.0001) == "12-24h"
    assert classify_lead_time_bucket(24.0) == "12-24h"
    assert classify_lead_time_bucket(24.0001) == "24-48h"
    assert classify_lead_time_bucket(48.0) == "24-48h"
    assert classify_lead_time_bucket(48.0001) == ">48h"
    assert classify_lead_time_bucket(None) is None


def test_descriptive_analysis_excludes_missing_and_counts_them() -> None:
    snaps = (
        _snap(token="ok", p_market=0.20),
        _snap(token="missing_p", p_market=None),
        _snap(token="no_model", p_market=0.10),
    )
    result = DescriptiveBacktester().evaluate(
        snapshots=snaps,
        model_probabilities={"ok": 0.35, "missing_p": 0.40},
        thresholds=EDGE_THRESHOLDS,
        split_name="validation",
    )
    analysis = result.descriptive_analysis
    assert analysis is not None
    assert analysis.valid_candidates == 1
    assert analysis.excluded_missing_model_or_market == 2
    assert analysis.average_model_probability == pytest.approx(0.35)
    assert analysis.average_market_probability == pytest.approx(0.20)
    assert analysis.average_raw_edge == pytest.approx(0.15)
    assert result.average_model_probability == pytest.approx(0.35)
    assert result.average_descriptive_market_probability == pytest.approx(0.20)
    assert result.average_raw_edge == pytest.approx(0.15)


def test_descriptive_bucket_stats_and_fixed_order() -> None:
    snaps = (
        _snap(token="a", p_market=0.005, lead_hours=0.5, settlement="No"),  # edge 0.045
        _snap(token="b", p_market=0.02, lead_hours=1.0, settlement="Yes"),  # edge 0.08
        _snap(token="c", p_market=0.04, lead_hours=8.0, settlement="Yes"),  # edge 0.16
        _snap(token="d", p_market=0.08, lead_hours=30.0, settlement=None),  # edge 0.22
        _snap(token="e", p_market=0.30, lead_hours=60.0, settlement="No"),  # edge -0.05
        _snap(token="f", p_market=0.12, lead_hours=None, settlement="Yes"),  # edge 0.03
    )
    probs = {
        "a": 0.05,
        "b": 0.10,
        "c": 0.20,
        "d": 0.30,
        "e": 0.25,
        "f": 0.15,
    }
    result = DescriptiveBacktester().evaluate(
        snapshots=snaps,
        model_probabilities=probs,
        thresholds=EDGE_THRESHOLDS,
        split_name="test",
    )
    analysis = result.descriptive_analysis
    assert analysis is not None
    assert analysis.valid_candidates == 6
    assert [row.bucket for row in analysis.raw_edge_buckets] == [
        "<=0%",
        "0-5%",
        "5-10%",
        "10-15%",
        "15-20%",
        ">20%",
    ]
    by_edge = {row.bucket: row for row in analysis.raw_edge_buckets}
    assert by_edge["<=0%"].n == 1
    assert by_edge["0-5%"].n == 2  # a=0.045, f=0.03
    assert by_edge["5-10%"].n == 1  # b=0.08
    assert by_edge["15-20%"].n == 1  # c=0.16
    assert by_edge[">20%"].n == 1  # d=0.22
    assert by_edge["5-10%"].settled_yes_count == 1
    assert by_edge["5-10%"].settled_yes_fraction == pytest.approx(1.0)
    assert by_edge[">20%"].settled_yes_count == 0
    assert by_edge[">20%"].settled_yes_fraction == pytest.approx(0.0)
    assert by_edge["0-5%"].mean_model_probability == pytest.approx((0.05 + 0.15) / 2)
    assert by_edge["0-5%"].mean_market_probability == pytest.approx((0.005 + 0.12) / 2)
    assert by_edge["0-5%"].mean_raw_edge == pytest.approx((0.045 + 0.03) / 2)

    assert [row.bucket for row in analysis.entry_price_buckets] == [
        "<1c",
        "1-3c",
        "3-5c",
        "5-10c",
        "10-25c",
        ">25c",
    ]
    by_entry = {row.bucket: row for row in analysis.entry_price_buckets}
    assert by_entry["<1c"].n == 1
    assert by_entry["1-3c"].n == 1
    assert by_entry["3-5c"].n == 1
    assert by_entry["5-10c"].n == 1
    assert by_entry["10-25c"].n == 1
    assert by_entry[">25c"].n == 1

    assert [row.bucket for row in analysis.lead_time_buckets] == [
        ">48h",
        "24-48h",
        "12-24h",
        "6-12h",
        "1-6h",
        "<1h",
    ]
    by_lead = {row.bucket: row for row in analysis.lead_time_buckets}
    assert by_lead["<1h"].n == 1
    assert by_lead["1-6h"].n == 1
    assert by_lead["6-12h"].n == 1
    assert by_lead["24-48h"].n == 1
    assert by_lead[">48h"].n == 1
    assert sum(row.n for row in analysis.lead_time_buckets) == 5
    assert analysis.missing_lead_hours_count == 1


def test_group_breakdowns_city_station_month_season_region() -> None:
    snaps = (
        _snap(
            token="p1",
            p_market=0.10,
            city="paris",
            station="LFPG",
            kind="exact",
            event_date="2026-04-10",
        ),
        _snap(
            token="p2",
            p_market=0.20,
            city="paris",
            station="LFPG",
            kind="below",
            event_date="2026-04-11",
        ),
        _snap(
            token="n1",
            p_market=0.15,
            city="new york",
            station="KLGA",
            kind="above",
            event_date="2026-01-05",
        ),
        _snap(
            token="u1",
            p_market=0.05,
            city=None,
            station=None,
            kind=None,
            event_date="2026-09-01",
        ),
    )
    probs = {"p1": 0.20, "p2": 0.30, "n1": 0.25, "u1": 0.10}
    result = DescriptiveBacktester().evaluate(
        snapshots=snaps,
        model_probabilities=probs,
        thresholds=EDGE_THRESHOLDS,
        split_name="validation",
    )
    analysis = result.descriptive_analysis
    assert analysis is not None
    cities = [row.bucket for row in analysis.by_city]
    assert cities == sorted(cities)
    assert {row.bucket: row.n for row in analysis.by_city} == {
        "new york": 1,
        "paris": 2,
        "unknown": 1,
    }
    assert {row.bucket: row.n for row in analysis.by_station_icao} == {
        "KLGA": 1,
        "LFPG": 2,
        "unknown": 1,
    }
    assert {row.bucket: row.n for row in analysis.by_event_month} == {
        "2026-01": 1,
        "2026-04": 2,
        "2026-09": 1,
    }
    assert {row.bucket: row.n for row in analysis.by_season} == {
        "autumn": 1,
        "spring": 2,
        "winter": 1,
    }
    assert {row.bucket: row.n for row in analysis.by_bucket_region} == {
        "center": 1,
        "tail": 2,
        "unknown": 1,
    }


def test_null_executable_metrics_and_no_zero_substitution() -> None:
    result = DescriptiveBacktester().evaluate(
        snapshots=(_snap(token="a", p_market=0.10),),
        model_probabilities={"a": 0.20},
        thresholds=EDGE_THRESHOLDS,
        split_name="test",
    )
    assert result.executable_trades == 0
    assert result.pnl is None
    assert result.roi is None
    assert result.max_drawdown is None
    assert result.profit_factor is None
    assert result.win_rate is None
    assert result.average_executable_entry_price is None
    assert result.selected_threshold is None
    empty = DescriptiveBacktester().evaluate(
        snapshots=(_snap(token="x", p_market=None),),
        model_probabilities={},
        thresholds=EDGE_THRESHOLDS,
        split_name="test",
    )
    assert empty.candidates == 0
    assert empty.average_model_probability is None
    assert empty.average_descriptive_market_probability is None
    assert empty.average_raw_edge is None
    assert empty.descriptive_analysis is not None
    assert empty.descriptive_analysis.valid_candidates == 0
    for row in empty.descriptive_analysis.raw_edge_buckets:
        assert row.n == 0
        assert row.mean_model_probability is None
        assert row.mean_market_probability is None
        assert row.mean_raw_edge is None
        assert row.settled_yes_fraction is None


def test_backtest_report_includes_descriptive_breakdowns_and_model_metrics(
    tmp_path: Path,
) -> None:
    snaps = tuple(
        _snap(
            token=f"t{i}",
            p_market=0.10 + 0.01 * i,
            event_date=f"2026-07-{10 + i:02d}",
            lead_hours=1.0 if i == 0 else 24.0,
            settlement="Yes" if i % 2 == 0 else "No",
        )
        for i in range(6)
    )
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    result_a = run_phase3(snaps, output_dir=out_a)
    run_phase3(snaps, output_dir=out_b)
    assert result_a.backtest_test.descriptive_analysis is not None
    bt_json = json.loads((out_a / "reports" / "phase3_backtest.json").read_text(encoding="utf-8"))
    bt_md = (out_a / "reports" / "phase3_backtest.md").read_text(encoding="utf-8")
    for split_name in ("validation", "test"):
        block = bt_json[split_name]
        assert (
            block["average_descriptive_market_probability"] is not None or block["candidates"] == 0
        )
        assert block["average_model_probability"] is not None or block["candidates"] == 0
        assert block["average_raw_edge"] is not None or block["candidates"] == 0
        assert block["average_executable_entry_price"] is None
        assert block["win_rate"] is None
        assert block["pnl"] is None
        assert block["roi"] is None
        assert block["max_drawdown"] is None
        assert block["profit_factor"] is None
        assert block["selected_threshold"] is None
        descriptive = block["descriptive_analysis"]
        assert "raw_edge_buckets" in descriptive
        assert "entry_price_buckets" in descriptive
        assert "lead_time_buckets" in descriptive
        assert "by_city" in descriptive
        assert "by_station_icao" in descriptive
        assert "by_event_month" in descriptive
        assert "by_season" in descriptive
        assert "by_bucket_region" in descriptive
        assert "excluded_missing_model_or_market" in descriptive
        model_metrics = block["model_metrics"]
        assert "multiclass_brier" in model_metrics
        assert "clipped_log_loss" in model_metrics
        assert "ece" in model_metrics
    assert "descriptive" in bt_md.lower()
    assert "raw_edge_buckets" in bt_md or "raw_edge" in bt_md
    assert "not executable" in bt_md.lower() or "non-executable" in bt_md.lower()
    assert "MEASURED DATA" in bt_md
    assert (out_a / "reports" / "phase3_backtest.md").read_bytes() == (
        out_b / "reports" / "phase3_backtest.md"
    ).read_bytes()
    assert (out_a / "reports" / "phase3_backtest.json").read_bytes() == (
        out_b / "reports" / "phase3_backtest.json"
    ).read_bytes()


def test_descriptive_json_encoding_is_stable(tmp_path: Path) -> None:
    result = DescriptiveBacktester().evaluate(
        snapshots=(_snap(token="a", p_market=0.10, lead_hours=1.0),),
        model_probabilities={"a": 0.20},
        thresholds=EDGE_THRESHOLDS,
        split_name="test",
    )
    payload = {
        "test": {
            "descriptive_analysis": result.descriptive_analysis,
            "average_raw_edge": result.average_raw_edge,
        }
    }
    path_md = tmp_path / "x.md"
    path_json = tmp_path / "x.json"
    write_report_pair(path_md, path_json, "# x\n", payload)
    write_report_pair(path_md, path_json, "# x\n", payload)
    first = path_json.read_bytes()
    write_report_pair(path_md, path_json, "# x\n", payload)
    assert path_json.read_bytes() == first
