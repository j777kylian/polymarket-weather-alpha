"""Fixture integration: collect + run without network."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner

from tests.fakes import RecordingGetTransport
from weather_alpha.cli import main
from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.research.collect import Phase3CollectOptions, Phase3Collector
from weather_alpha.research.run import load_quarantine, load_snapshots_from_jsonl, run_phase3

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STATIONS = Path(__file__).resolve().parents[1] / "config" / "stations.yaml"


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _phase3_routes() -> dict[str, object]:
    paris_search = _load("phase3_gamma_search_paris.json")
    ny_search = _load("phase3_gamma_search_ny.json")
    empty: dict[str, list[object]] = {"events": [], "markets": []}

    def search(params: dict[str, object]) -> object:
        query = str(params.get("q") or "").lower()
        if "paris" in query:
            return paris_search
        if "new york" in query:
            return ny_search
        return empty

    def prices(params: dict[str, object]) -> object:
        token = str(params.get("market") or "")
        # Decision timestamps are mid-July 2026; keep points at/before those times.
        if token in {"yes-paris-30", "yes-paris-31"}:
            return {
                "history": [
                    {"t": int(datetime(2026, 7, 13, 12, 0, tzinfo=UTC).timestamp()), "p": 0.35},
                    {"t": int(datetime(2026, 7, 13, 21, 0, tzinfo=UTC).timestamp()), "p": 0.41},
                    {"t": int(datetime(2026, 7, 14, 12, 0, tzinfo=UTC).timestamp()), "p": 0.99},
                ]
            }
        if token in {"yes-ny-86", "yes-ny-87"}:
            return {
                "history": [
                    {"t": int(datetime(2026, 7, 13, 12, 0, tzinfo=UTC).timestamp()), "p": 0.12},
                    {"t": int(datetime(2026, 7, 14, 3, 0, tzinfo=UTC).timestamp()), "p": 0.18},
                ]
            }
        return {"history": []}

    def single_run(params: dict[str, object]) -> object:
        lat = float(str(params.get("latitude") or 0))
        if abs(lat - 49.0097) < 0.1:
            return _load("phase3_single_run_lfpg.json")
        return _load("phase3_single_run_klga.json")

    def archive(params: dict[str, object]) -> object:
        lat = float(str(params.get("latitude") or 0))
        if abs(lat - 49.0097) < 0.1:
            return _load("phase3_archive_lfpg.json")
        return _load("phase3_archive_klga.json")

    return {
        "/public-search": search,
        "/prices-history": prices,
        "single-runs-api.open-meteo.com": single_run,
        "archive-api.open-meteo.com": archive,
    }


def test_fixture_collect_and_run_writes_parquet_and_reports(tmp_path: Path) -> None:
    transport = RecordingGetTransport(_phase3_routes())
    http = ReadOnlyHttpClient(transport=transport, max_retries=0)
    out = tmp_path / "phase3"
    collector = Phase3Collector(
        http=http,
        retrieved_at=datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
    )
    report = collector.collect(
        Phase3CollectOptions(
            start_date="2026-07-14",
            end_date="2026-07-16",
            output_root=out,
            max_search_pages=2,
            price_fidelity_minutes=60,
            forecast_lead_hours=24,
            cities=("paris", "new york"),
            stations_file=STATIONS,
        )
    )
    assert report.snapshots_written >= 2
    assert (out / "phase3_snapshots.parquet").is_file()
    assert (out / "phase3_snapshots.jsonl").is_file()
    assert (out / "phase3_source_manifest.json").is_file()
    manifest = json.loads((out / "phase3_source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["search_limit_per_type"] == 50
    search_urls = [url for _method, url in transport.calls if "/public-search" in url]
    assert search_urls
    assert all("limit_per_type=50" in url for url in search_urls)
    quarantine = json.loads((out / "phase3_quarantine.json").read_text(encoding="utf-8"))
    assert any(
        "LFPB" in str(row.get("reason", "")) or "unknown station" in str(row.get("reason", ""))
        for row in quarantine
    )

    snapshots = load_snapshots_from_jsonl(out / "phase3_snapshots.jsonl")
    cities = {snap.city for snap in snapshots}
    assert "new york" in cities
    assert "paris" in cities
    # Root cause of snapshots_written==1: a lone NY market with Yes-outcomePrices=0 was
    # quarantined as event_settlement_unresolved (yes_count==0 for the whole group).
    # Fixtures now keep adjacent native buckets with exactly one Yes per event family,
    # distinct parent event ids (so July-16 LFPB cannot date-conflict July-15), and
    # target-date weather coverage for LFPG/KLGA.
    assert len(snapshots) >= 2
    assert report.snapshots_written == len(snapshots)
    assert {snap.event_date for snap in snapshots} == {"2026-07-15"}
    for snap in snapshots:
        assert snap.forecast_daily_max_c is not None
        assert snap.diagnostic_actual_max_c is not None
        assert snap.best_ask is None
        assert snap.executable_entry_price is None
        assert snap.decision_ts.tzinfo is not None
        assert snap.observation_max_so_far_c is None
        assert snap.observation_as_of is None
        assert snap.forecast_hourly
        assert all(hour.valid_time_utc.tzinfo is not None for hour in snap.forecast_hourly)
        if snap.weather_available_at is not None:
            assert snap.weather_available_at <= snap.decision_ts
        if snap.market_probability is not None:
            assert snap.market_probability != 0.99  # future price skipped for Paris
            assert snap.market_price_observed_at is not None
            assert snap.market_price_observed_at <= snap.decision_ts
            assert snap.price_request_url is not None
            assert snap.price_raw_path is not None
            assert snap.price_content_sha256

    run_out = tmp_path / "phase3-run"
    result = run_phase3(
        snapshots,
        output_dir=run_out,
        quarantined=load_quarantine(out / "phase3_quarantine.json"),
        collect_manifest=manifest,
    )
    reports = run_out / "reports"
    for name in (
        "phase3_dataset_audit",
        "phase3_model_calibration",
        "phase3_backtest",
        "phase3_tail_alpha",
    ):
        md = reports / f"{name}.md"
        js = reports / f"{name}.json"
        assert md.is_file()
        assert js.is_file()
        text = md.read_text(encoding="utf-8")
        for section in (
            "MEASURED DATA",
            "MODEL OUTPUT",
            "ASSUMPTIONS",
            "MISSING DATA",
            "INFERENCES",
        ):
            assert section in text
    assert result.backtest_test.executable_trades == 0
    assert result.backtest_test.pnl is None
    assert result.backtest_test.selected_threshold is None
    cal_json = json.loads((reports / "phase3_model_calibration.json").read_text(encoding="utf-8"))
    assert "baseline" in json.dumps(cal_json)
    assert "measured_data" in cal_json and "model_output" in cal_json
    sample_text = (reports / "phase3_model_calibration.md").read_text(encoding="utf-8").lower()
    assert "insufficient" in sample_text or "inconclusive" in sample_text
    assert "POST" not in {method for method, _url in transport.calls}
    assert all(method == "GET" for method, _url in transport.calls)


def test_reports_are_byte_deterministic(tmp_path: Path) -> None:
    transport = RecordingGetTransport(_phase3_routes())
    http = ReadOnlyHttpClient(transport=transport, max_retries=0)
    out = tmp_path / "src"
    Phase3Collector(http=http, retrieved_at=datetime(2026, 8, 1, 0, 0, tzinfo=UTC)).collect(
        Phase3CollectOptions(
            start_date="2026-07-14",
            end_date="2026-07-16",
            output_root=out,
            max_search_pages=2,
            cities=("paris", "new york"),
            stations_file=STATIONS,
        )
    )
    snapshots = load_snapshots_from_jsonl(out / "phase3_snapshots.jsonl")
    quarantined = load_quarantine(out / "phase3_quarantine.json")
    a = tmp_path / "a"
    b = tmp_path / "b"
    run_phase3(snapshots, output_dir=a, quarantined=quarantined)
    run_phase3(snapshots, output_dir=b, quarantined=quarantined)
    for name in (
        "phase3_dataset_audit.md",
        "phase3_dataset_audit.json",
        "phase3_model_calibration.md",
        "phase3_model_calibration.json",
        "phase3_backtest.md",
        "phase3_backtest.json",
        "phase3_tail_alpha.md",
        "phase3_tail_alpha.json",
    ):
        assert (a / "reports" / name).read_bytes() == (b / "reports" / name).read_bytes()


def test_phase3_cli_dry_run_and_bounds(tmp_path: Path) -> None:
    runner = CliRunner()
    dry = runner.invoke(
        main,
        [
            "phase3-collect",
            "--dry-run",
            "--start-date",
            "2026-07-01",
            "--end-date",
            "2026-07-10",
            "--output-root",
            str(tmp_path / "out"),
            "--max-search-pages",
            "2",
            "--city",
            "paris",
        ],
    )
    assert dry.exit_code == 0, dry.output
    assert "dry-run" in dry.output.lower()
    assert not (tmp_path / "out").exists()

    too_long = runner.invoke(
        main,
        [
            "phase3-collect",
            "--dry-run",
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-06-01",
            "--output-root",
            str(tmp_path / "out2"),
        ],
    )
    assert too_long.exit_code != 0

    zero_pages = runner.invoke(
        main,
        [
            "phase3-collect",
            "--dry-run",
            "--start-date",
            "2026-07-01",
            "--end-date",
            "2026-07-02",
            "--output-root",
            str(tmp_path / "out3"),
            "--max-search-pages",
            "0",
        ],
    )
    assert zero_pages.exit_code != 0

    zero_limit = runner.invoke(
        main,
        [
            "phase3-collect",
            "--dry-run",
            "--start-date",
            "2026-07-01",
            "--end-date",
            "2026-07-02",
            "--output-root",
            str(tmp_path / "out4"),
            "--search-limit-per-type",
            "0",
        ],
    )
    assert zero_limit.exit_code != 0
