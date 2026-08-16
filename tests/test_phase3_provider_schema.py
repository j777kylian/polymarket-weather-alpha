"""Blocker 3: HTTP-200 payloads must fail closed on schema drift."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes import RecordingGetTransport
from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.research.collect import Phase3CollectOptions, Phase3Collector
from weather_alpha.research.prices import parse_price_history_points
from weather_alpha.research.provider_schema import (
    ProviderSchemaError,
    SchemaValidationResult,
    validate_archive_payload,
    validate_gamma_search_payload,
    validate_prices_history_payload,
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
        "events": [{"id": "evt-paris-1"}],
    }


def _search_payload(markets: list[dict[str, object]]) -> dict[str, object]:
    return {
        "events": [
            {
                "id": "evt-paris-1",
                "slug": "highest-temperature-in-paris-on-july-15-2026",
                "markets": markets,
            }
        ],
        "markets": [],
    }


def _weather_routes() -> dict[str, object]:
    return {
        "single-runs-api.open-meteo.com": lambda _p: _load("phase3_single_run_lfpg.json"),
        "archive-api.open-meteo.com": lambda _p: _load("phase3_archive_lfpg.json"),
    }


def test_prices_history_valid_nonempty_and_empty() -> None:
    nonempty: dict[str, object] = {"history": [{"t": 1_720_000_000, "p": 0.41}]}
    empty: dict[str, object] = {"history": []}
    assert validate_prices_history_payload(nonempty).status == "ok"
    assert validate_prices_history_payload(empty).status == "empty"
    assert len(parse_price_history_points(nonempty)) == 1
    assert parse_price_history_points(empty) == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "upstream drift"},
        {"message": "nope"},
        {"history": "not-a-list"},
        {"history": [{"t": "bad", "p": 0.2}]},
        {"history": [{"p": 0.2}]},
        ["history"],
        None,
        12,
        {},
    ],
)
def test_prices_history_malformed_fail_closed(payload: object) -> None:
    result = validate_prices_history_payload(payload)
    assert result.status in {"malformed", "source_drift"}
    with pytest.raises(ProviderSchemaError):
        result.raise_for_status()
    # parse must not silently become empty success for error objects
    if isinstance(payload, dict) and "error" in payload:
        with pytest.raises(ProviderSchemaError):
            parse_price_history_points(payload)


def test_gamma_search_valid_empty_and_malformed() -> None:
    valid = _search_payload([_paris_market()])
    empty: dict[str, object] = {"events": [], "markets": []}
    assert validate_gamma_search_payload(valid).status == "ok"
    assert validate_gamma_search_payload(empty).status == "empty"
    bad_payloads: tuple[object, ...] = (
        {"error": "upstream drift"},
        {"events": "nope"},
        {"markets": {}},
        {"unexpected": True},
        [],
    )
    for payload in bad_payloads:
        result = validate_gamma_search_payload(payload)
        assert result.status in {"malformed", "source_drift"}


def test_single_run_and_archive_schema_validation() -> None:
    single = _load("phase3_single_run_lfpg.json")
    archive = _load("phase3_archive_lfpg.json")
    assert validate_single_run_payload(single).status == "ok"
    assert validate_archive_payload(archive).status == "ok"
    bad_payloads: tuple[object, ...] = (
        {"error": "drift"},
        {"hourly": []},
        None,
        {"daily": {}},
    )
    for payload in bad_payloads:
        assert validate_single_run_payload(payload).status in {"malformed", "source_drift"}
        assert validate_archive_payload(payload).status in {"malformed", "source_drift"}


def test_collect_counts_malformed_price_schema_separately(tmp_path: Path) -> None:
    routes: dict[str, object] = {
        "/public-search": _search_payload([_paris_market()]),
        "/prices-history": {"error": "upstream drift"},
        **_weather_routes(),
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
    assert report.price_schema_errors >= 1
    assert report.price_history_empty == 0
    assert report.price_http_errors == 0
    manifest = json.loads((tmp_path / "phase3_source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["price_schema_errors"] >= 1
    assert "price_schema_errors" in " ".join(report.notes)


def test_schema_validation_result_distinguishes_classes() -> None:
    ok = SchemaValidationResult(status="ok", provider="prices-history")
    empty = SchemaValidationResult(status="empty", provider="prices-history")
    malformed = SchemaValidationResult(status="malformed", provider="prices-history")
    http = SchemaValidationResult(status="http_failure", provider="prices-history")
    drift = SchemaValidationResult(status="source_drift", provider="prices-history")
    assert ok.status != empty.status
    assert malformed.status != empty.status
    assert http.status != malformed.status
    assert drift.status != ok.status
