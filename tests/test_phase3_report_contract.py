"""Blocker 4: primary JSON reports independently expose the research contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from weather_alpha.research.run import run_phase3
from weather_alpha.research.types import ResearchSnapshot

REQUIRED_SECTIONS = (
    "measured_data",
    "model_output",
    "assumptions",
    "missing_data",
    "inferences",
    "limitations",
)

PRIMARY_REPORTS = (
    "phase3_dataset_audit.json",
    "phase3_model_calibration.json",
    "phase3_backtest.json",
    "phase3_tail_alpha.json",
)


def _snap(
    *,
    token: str,
    event_date: str,
    bucket: str,
    settlement: str,
    event_id: str,
) -> ResearchSnapshot:
    return ResearchSnapshot(
        condition_id=f"{event_id}-{bucket}",
        market_id="m",
        token_id=token,
        city="paris",
        station_icao="LFPG",
        event_date=event_date,
        bucket_label=bucket,
        bucket_kind="exact",
        temperature_celsius_min=31.0,
        temperature_celsius_max=31.0,
        decision_ts=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        market_probability=0.4,
        executable_entry_price=None,
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        volume=None,
        liquidity=None,
        weather_issued_at=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 1, 6, 0, tzinfo=UTC),
        forecast_daily_max_c=30.0,
        observation_max_so_far_c=None,
        observation_as_of=None,
        settlement_label=settlement,
        diagnostic_actual_max_c=31.0,
        provenance_urls=(),
        raw_paths=(),
        content_hashes=(),
        limitations=("test",),
        event_id=event_id,
        forecast_lead_hours=1.0,
    )


def _mini_run(tmp_path: Path) -> Path:
    snapshots = (
        _snap(token="a", event_date="2026-07-01", bucket="30°C", settlement="Yes", event_id="e1"),
        _snap(token="b", event_date="2026-07-01", bucket="31°C", settlement="No", event_id="e1"),
        _snap(token="c", event_date="2026-07-08", bucket="30°C", settlement="Yes", event_id="e2"),
        _snap(token="d", event_date="2026-07-08", bucket="31°C", settlement="No", event_id="e2"),
        _snap(token="e", event_date="2026-07-15", bucket="30°C", settlement="Yes", event_id="e3"),
        _snap(token="f", event_date="2026-07-15", bucket="31°C", settlement="No", event_id="e3"),
    )
    run_phase3(
        snapshots,
        output_dir=tmp_path,
        collect_manifest={
            "start_date": "2026-07-01",
            "end_date": "2026-07-15",
            "cities": ["paris"],
            "max_search_pages": 2,
            "search_limit_per_type": 50,
            "discovered_outside_range": 4,
            "snapshots": 6,
            "quarantined": 1,
            "price_http_errors": 2,
            "price_history_empty": 1,
            "price_schema_errors": 0,
            "providers": [
                "polymarket-gamma-public-search",
                "polymarket-clob-prices-history",
                "open-meteo-single-runs",
                "open-meteo-archive",
            ],
            "usable_event_dates": ["2026-07-01", "2026-07-08", "2026-07-15"],
        },
    )
    return tmp_path / "reports"


def test_primary_json_reports_expose_required_contract_sections(tmp_path: Path) -> None:
    reports = _mini_run(tmp_path)
    for name in PRIMARY_REPORTS:
        payload = json.loads((reports / name).read_text(encoding="utf-8"))
        for section in REQUIRED_SECTIONS:
            assert section in payload, f"{name} missing {section}"
            assert isinstance(payload[section], dict)
        assert "common_research_context" in payload, f"{name} missing common_research_context"
        ctx = payload["common_research_context"]
        assert isinstance(ctx, dict)
        for key in (
            "providers",
            "requested_range",
            "chronological_split",
            "counts",
            "exclusions",
            "quarantines",
            "model_identity",
            "executable_state",
            "assumptions",
            "missing_data",
            "limitations",
            "metrics",
            "descriptive_price_vs_executable",
            "standalone_caveats",
        ):
            assert key in ctx, f"{name} common_research_context missing {key}"
        caveats = ctx["standalone_caveats"]
        assert caveats["descriptive_price_not_executable"] is True
        assert caveats["asks_order_books_absent"] is True
        assert caveats["gamma_survivorship_bias"] is True
        assert caveats["wunderground_settlement_caveat"] is True
        assert caveats["point_in_time_metar_absent"] is True
        assert caveats["sample_or_checkpoint_limits"] is True
        assert caveats["no_alpha_inference"] is True


def test_each_primary_report_independently_carries_contract_caveats(tmp_path: Path) -> None:
    reports = _mini_run(tmp_path)
    contexts = []
    for name in PRIMARY_REPORTS:
        payload = json.loads((reports / name).read_text(encoding="utf-8"))
        text = json.dumps(payload).lower()
        assert "provider" in text
        assert "requested" in text or "range" in text or "date" in text
        assert "train" in text and "validation" in text and "test" in text
        assert "quarantin" in text or "exclud" in text
        assert "ask" in text or "order book" in text or "orderbook" in text
        assert "metar" in text or "observation" in text or "point-in-time" in text
        assert "settlement" in text or "wunderground" in text
        assert "survivorship" in text or "gamma" in text
        assert "sample" in text or "checkpoint" in text or "operational" in text
        assert "alpha" in text or "executable" in text
        assert payload["inferences"]
        assert payload["missing_data"] is not None
        assert payload["limitations"] is not None
        ctx = payload["common_research_context"]
        contexts.append(json.dumps(ctx, sort_keys=True))
        assert ctx["metrics"] is not None
    # Same deterministic context object copied into every primary report.
    assert len(set(contexts)) == 1


def test_json_contract_exposes_providers_ranges_counts_and_caveats(tmp_path: Path) -> None:
    reports = _mini_run(tmp_path)
    audit = json.loads((reports / "phase3_dataset_audit.json").read_text(encoding="utf-8"))
    measured = audit["measured_data"]
    assert "providers" in measured
    assert "requested_range" in measured or "requested_dates" in measured
    assert "market_counts" in measured or "snapshots" in measured or "markets" in measured
    assert "excluded" in measured or "quarantined" in measured or "exclusions" in measured
    limitations = audit["limitations"]
    assert (
        "survivorship" in json.dumps(limitations).lower()
        or "gamma" in json.dumps(limitations).lower()
    )
    assert (
        "settlement" in json.dumps(limitations).lower()
        or "wunderground" in json.dumps(limitations).lower()
    )
    assert (
        "asks" in json.dumps(limitations).lower() or "order book" in json.dumps(limitations).lower()
    )

    cal = json.loads((reports / "phase3_model_calibration.json").read_text(encoding="utf-8"))
    assert "model_type" in cal["model_output"] or "models" in cal["model_output"]
    assert "train" in json.dumps(cal["measured_data"]).lower()
    assert "validation" in json.dumps(cal["measured_data"]).lower()
    assert "test" in json.dumps(cal["measured_data"]).lower()

    bt = json.loads((reports / "phase3_backtest.json").read_text(encoding="utf-8"))
    assert (
        "executable" in json.dumps(bt["missing_data"]).lower()
        or "asks" in json.dumps(bt["missing_data"]).lower()
    )
    assert bt["inferences"]
    assert bt["assumptions"]

    tail = json.loads((reports / "phase3_tail_alpha.json").read_text(encoding="utf-8"))
    assert tail["measured_data"]
    assert "executable_survival" in json.dumps(tail)
    assert tail["limitations"]


def test_json_contract_regeneration_is_byte_identical(tmp_path: Path) -> None:
    first = _mini_run(tmp_path / "a")
    second = _mini_run(tmp_path / "b")
    for name in PRIMARY_REPORTS:
        assert (first / name).read_bytes() == (second / name).read_bytes()
