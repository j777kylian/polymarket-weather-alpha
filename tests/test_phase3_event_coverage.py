"""Target event-date coverage: schema-valid Phase3-ineligible vs empty vs malformed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes import RecordingGetTransport
from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.research.collect import Phase3CollectOptions, Phase3Collector
from weather_alpha.research.event_coverage import (
    NO_USABLE_EVENT_COVERAGE,
    evaluate_archive_event_coverage,
    evaluate_single_run_event_coverage,
)
from weather_alpha.research.provider_schema import (
    ProviderSchemaError,
    validate_archive_payload,
    validate_single_run_payload,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STATIONS = Path(__file__).resolve().parents[1] / "config" / "stations.yaml"


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _paris_market() -> dict[str, object]:
    return {
        "id": "0xinrange",
        "question": "Highest temperature in Paris on July 15, 2026?",
        "conditionId": "0xinrange",
        "slug": "highest-temperature-in-paris-on-july-15-2026",
        "description": (
            "Station at Paris Charles de Gaulle Airport LFPG. "
            "https://www.wunderground.com/history/daily/fr/paris/LFPG."
        ),
        "groupItemTitle": "31°C",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-in", "no-in"]',
        "outcomePrices": '["1", "0"]',
        "closed": True,
        "resolved": True,
        "active": False,
        "events": [{"id": "evt-cov-1"}],
    }


def _search_payload(markets: list[dict[str, object]]) -> dict[str, object]:
    return {
        "events": [{"id": "evt-cov-1", "markets": markets}],
        "markets": [],
    }


def test_single_run_target_date_non_null_eligible() -> None:
    payload = _load("phase3_single_run_lfpg.json")
    schema = validate_single_run_payload(payload)
    assert schema.status == "ok"
    coverage = evaluate_single_run_event_coverage(payload, event_date="2026-07-15")
    assert coverage.usable is True
    assert coverage.status == "eligible"
    assert coverage.reason is None


def test_single_run_absent_target_date_ineligible() -> None:
    payload = _load("phase3_single_run_lfpg.json")
    assert validate_single_run_payload(payload).status == "ok"
    coverage = evaluate_single_run_event_coverage(payload, event_date="2026-07-16")
    assert coverage.usable is False
    assert coverage.status == "ineligible"
    assert coverage.reason == NO_USABLE_EVENT_COVERAGE
    assert coverage.phase3_eligibility == "ineligible"


def test_single_run_all_null_target_temps_ineligible() -> None:
    payload = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 7200,
        "hourly_units": {"temperature_2m": "°C"},
        "hourly": {
            "time": ["2026-07-15T00:00", "2026-07-15T12:00"],
            "temperature_2m": [None, None],
        },
    }
    assert validate_single_run_payload(payload).status == "ok"
    coverage = evaluate_single_run_event_coverage(payload, event_date="2026-07-15")
    assert coverage.status == "ineligible"
    assert coverage.reason == NO_USABLE_EVENT_COVERAGE
    assert "null" in (coverage.detail or "")


def test_archive_exact_target_valid_max_eligible() -> None:
    payload = _load("phase3_archive_lfpg.json")
    assert validate_archive_payload(payload).status == "ok"
    coverage = evaluate_archive_event_coverage(payload, event_date="2026-07-15")
    assert coverage.usable is True


def test_archive_absent_or_null_target_ineligible() -> None:
    payload = _load("phase3_archive_lfpg.json")
    absent = evaluate_archive_event_coverage(payload, event_date="2026-07-16")
    assert absent.status == "ineligible"
    assert absent.reason == NO_USABLE_EVENT_COVERAGE

    null_max = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 7200,
        "daily_units": {"temperature_2m_max": "°C"},
        "daily": {"time": ["2026-07-15"], "temperature_2m_max": [None]},
    }
    assert validate_archive_payload(null_max).status == "ok"
    null_cov = evaluate_archive_event_coverage(null_max, event_date="2026-07-15")
    assert null_cov.status == "ineligible"
    assert null_cov.reason == NO_USABLE_EVENT_COVERAGE


def test_aligned_empties_are_valid_empty_no_coverage() -> None:
    single_empty = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 7200,
        "hourly_units": {"temperature_2m": "°C"},
        "hourly": {"time": [], "temperature_2m": []},
    }
    archive_empty = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 7200,
        "daily_units": {"temperature_2m_max": "°C"},
        "daily": {"time": [], "temperature_2m_max": []},
    }
    assert validate_single_run_payload(single_empty).status == "empty"
    assert validate_archive_payload(archive_empty).status == "empty"
    single_cov = evaluate_single_run_event_coverage(single_empty, event_date="2026-07-15")
    archive_cov = evaluate_archive_event_coverage(archive_empty, event_date="2026-07-15")
    assert single_cov.status == "empty"
    assert archive_cov.status == "empty"
    assert single_cov.reason == NO_USABLE_EVENT_COVERAGE
    assert archive_cov.reason == NO_USABLE_EVENT_COVERAGE


def test_malformed_remains_provider_schema_error() -> None:
    bad: dict[str, object] = {"hourly": []}
    result = validate_single_run_payload(bad)
    assert result.status == "malformed"
    with pytest.raises(ProviderSchemaError):
        result.raise_for_status()
    archive_bad: dict[str, object] = {"daily": {}}
    archive_result = validate_archive_payload(archive_bad)
    assert archive_result.status == "malformed"
    with pytest.raises(ProviderSchemaError):
        archive_result.raise_for_status()


def test_collect_counts_single_run_no_usable_event_coverage(tmp_path: Path) -> None:
    # Schema-valid hours exist but not for the market event date.
    single = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 7200,
        "hourly_units": {"temperature_2m": "°C"},
        "hourly": {
            "time": ["2026-07-14T00:00", "2026-07-14T12:00"],
            "temperature_2m": [20.0, 25.0],
        },
    }
    archive = _load("phase3_archive_lfpg.json")
    routes: dict[str, object] = {
        "/public-search": _search_payload([_paris_market()]),
        "/prices-history": {"history": []},
        "single-runs-api.open-meteo.com": single,
        "archive-api.open-meteo.com": archive,
    }
    report = Phase3Collector(
        http=ReadOnlyHttpClient(transport=RecordingGetTransport(routes), max_retries=0),
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    ).collect(
        Phase3CollectOptions(
            start_date="2026-07-15",
            end_date="2026-07-15",
            output_root=tmp_path,
            max_search_pages=1,
            cities=("paris",),
            stations_file=STATIONS,
        )
    )
    assert report.single_run_no_usable_event_coverage >= 1
    assert report.single_run_schema_errors == 0
    assert report.snapshots_written == 0
    quarantine = json.loads((tmp_path / "phase3_quarantine.json").read_text(encoding="utf-8"))
    assert any(NO_USABLE_EVENT_COVERAGE in str(row.get("reason", "")) for row in quarantine)
    assert any(
        "raw=" in str(row.get("reason", "")) or "raw=" in str(row.get("details", ""))
        for row in quarantine
    )
    manifest = json.loads((tmp_path / "phase3_source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["single_run_no_usable_event_coverage"] >= 1
    assert manifest["single_run_schema_errors"] == 0


def test_collect_counts_archive_no_usable_event_coverage(tmp_path: Path) -> None:
    single = _load("phase3_single_run_lfpg.json")
    archive = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 7200,
        "daily_units": {"temperature_2m_max": "°C"},
        "daily": {"time": ["2026-07-14"], "temperature_2m_max": [28.0]},
    }
    routes: dict[str, object] = {
        "/public-search": _search_payload([_paris_market()]),
        "/prices-history": {"history": []},
        "single-runs-api.open-meteo.com": single,
        "archive-api.open-meteo.com": archive,
    }
    report = Phase3Collector(
        http=ReadOnlyHttpClient(transport=RecordingGetTransport(routes), max_retries=0),
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    ).collect(
        Phase3CollectOptions(
            start_date="2026-07-15",
            end_date="2026-07-15",
            output_root=tmp_path,
            max_search_pages=1,
            cities=("paris",),
            stations_file=STATIONS,
        )
    )
    assert report.archive_no_usable_event_coverage >= 1
    assert report.archive_schema_errors == 0
    assert report.snapshots_written == 0
    quarantine = json.loads((tmp_path / "phase3_quarantine.json").read_text(encoding="utf-8"))
    assert any(NO_USABLE_EVENT_COVERAGE in str(row.get("reason", "")) for row in quarantine)
    manifest = json.loads((tmp_path / "phase3_source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["archive_no_usable_event_coverage"] >= 1
    assert manifest["archive_schema_errors"] == 0
