"""Phase 3 collection integrity: discovery coverage, HTTP status, archive leakage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.fakes import RecordingGetTransport
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyHttpError, ReadOnlyResponse
from weather_alpha.research.collect import Phase3CollectOptions, Phase3Collector
from weather_alpha.research.dataset import read_snapshots_parquet, row_to_snapshot
from weather_alpha.research.run import load_snapshots_from_jsonl

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STATIONS = Path(__file__).resolve().parents[1] / "config" / "stations.yaml"


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _paris_market(
    *, condition_id: str, date_label: str, iso_date: str, token: str
) -> dict[str, object]:
    month_name, day, year = date_label.split()
    return {
        "id": condition_id,
        "question": f"Highest temperature in Paris on {month_name} {day}, {year}?",
        "conditionId": condition_id,
        "slug": f"highest-temperature-in-paris-on-{month_name.lower()}-{day}-{year}",
        "description": (
            "Station at Paris Charles de Gaulle Airport LFPG. "
            "https://www.wunderground.com/history/daily/fr/paris/LFPG."
        ),
        "groupItemTitle": "31°C",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{token}", "no-{token}"]',
        "outcomePrices": '["1", "0"]',
        "closed": True,
        "resolved": True,
        "active": False,
        "startDate": f"{iso_date}T00:00:00Z",
        "endDate": f"{iso_date}T23:59:59Z",
        "events": [{"id": condition_id}],
    }


def _search_payload(markets: list[dict[str, object]]) -> dict[str, object]:
    return {"events": [{"id": "e-mix", "markets": markets}], "markets": []}


def _weather_routes() -> dict[str, object]:
    def single_run(_params: dict[str, object]) -> object:
        return _load("phase3_single_run_lfpg.json")

    def archive(_params: dict[str, object]) -> object:
        return _load("phase3_archive_lfpg.json")

    return {
        "single-runs-api.open-meteo.com": single_run,
        "archive-api.open-meteo.com": archive,
    }


def test_public_search_uses_bounded_limit_per_type_50(tmp_path: Path) -> None:
    in_range = _paris_market(
        condition_id="0xinrange",
        date_label="July 15 2026",
        iso_date="2026-07-15",
        token="yes-in",
    )
    routes: dict[str, object] = {
        "/public-search": _search_payload([in_range]),
        "/prices-history": {
            "history": [
                {"t": int(datetime(2026, 7, 13, 21, 0, tzinfo=UTC).timestamp()), "p": 0.41},
            ]
        },
        **_weather_routes(),
    }
    transport = RecordingGetTransport(routes)
    collector = Phase3Collector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    collector.collect(
        Phase3CollectOptions(
            start_date="2026-07-15",
            end_date="2026-07-15",
            output_root=tmp_path,
            max_search_pages=1,
            cities=("paris",),
            stations_file=STATIONS,
        )
    )
    search_urls = [url for method, url in transport.calls if "/public-search" in url]
    assert search_urls
    for url in search_urls:
        params = parse_qs(urlsplit(url).query)
        assert params.get("limit_per_type") == ["50"]


def test_out_of_range_discovered_markets_are_counted_not_quarantined(tmp_path: Path) -> None:
    in_range = _paris_market(
        condition_id="0xinrange",
        date_label="July 15 2026",
        iso_date="2026-07-15",
        token="yes-in",
    )
    outside = [
        _paris_market(
            condition_id=f"0xout{index}",
            date_label="February 01 2026",
            iso_date="2026-02-01",
            token=f"yes-out-{index}",
        )
        for index in range(8)
    ]
    routes: dict[str, object] = {
        "/public-search": _search_payload([in_range, *outside]),
        "/prices-history": {
            "history": [
                {"t": int(datetime(2026, 7, 13, 21, 0, tzinfo=UTC).timestamp()), "p": 0.41},
            ]
        },
        **_weather_routes(),
    }
    transport = RecordingGetTransport(routes)
    report = Phase3Collector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
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
    assert report.discovered_outside_range == 8
    quarantine = json.loads((tmp_path / "phase3_quarantine.json").read_text(encoding="utf-8"))
    assert not any(
        "outside requested range" in str(row.get("reason", "")).lower() for row in quarantine
    )
    manifest = json.loads((tmp_path / "phase3_source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["discovered_outside_range"] == 8
    assert manifest["search_limit_per_type"] == 50
    assert report.snapshots_written >= 1


def test_search_http_4xx_is_not_parsed_as_empty_success(tmp_path: Path) -> None:
    transport = RecordingGetTransport(
        {
            "/public-search": ReadOnlyResponse(
                status_code=404,
                url="https://gamma-api.polymarket.com/public-search",
                headers={},
                content=b'{"events":[],"markets":[]}',
            )
        }
    )
    collector = Phase3Collector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    with pytest.raises(ReadOnlyHttpError, match="404"):
        collector.collect(
            Phase3CollectOptions(
                start_date="2026-07-15",
                end_date="2026-07-15",
                output_root=tmp_path,
                max_search_pages=1,
                cities=("paris",),
                stations_file=STATIONS,
            )
        )
    assert not (tmp_path / "phase3_snapshots.jsonl").exists()


def test_price_http_4xx_body_is_not_used_and_probability_stays_null(tmp_path: Path) -> None:
    in_range = _paris_market(
        condition_id="0xinrange",
        date_label="July 15 2026",
        iso_date="2026-07-15",
        token="yes-in",
    )
    poison = {
        "history": [{"t": int(datetime(2026, 7, 13, 21, 0, tzinfo=UTC).timestamp()), "p": 0.0}]
    }
    routes: dict[str, object] = {
        "/public-search": _search_payload([in_range]),
        "/prices-history": ReadOnlyResponse(
            status_code=404,
            url="https://clob.polymarket.com/prices-history",
            headers={},
            content=json.dumps(poison).encode("utf-8"),
        ),
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
    assert report.snapshots_written >= 1
    assert report.price_http_errors >= 1
    assert report.price_history_empty == 0
    snapshots = load_snapshots_from_jsonl(tmp_path / "phase3_snapshots.jsonl")
    assert snapshots[0].market_probability is None
    assert snapshots[0].market_price_observed_at is None
    assert snapshots[0].best_ask is None
    assert snapshots[0].price_request_url is None
    limitation_text = " ".join(snapshots[0].limitations).lower()
    assert "http" in limitation_text
    assert "not treated as empty" in limitation_text
    manifest = json.loads((tmp_path / "phase3_source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["price_http_errors"] == report.price_http_errors
    assert manifest["price_history_empty"] == 0


def test_price_provenance_and_observed_at_roundtrip(tmp_path: Path) -> None:
    in_range = _paris_market(
        condition_id="0xinrange",
        date_label="July 15 2026",
        iso_date="2026-07-15",
        token="yes-in",
    )
    observed = datetime(2026, 7, 13, 21, 0, tzinfo=UTC)
    routes: dict[str, object] = {
        "/public-search": _search_payload([in_range]),
        "/prices-history": {"history": [{"t": int(observed.timestamp()), "p": 0.41}]},
        **_weather_routes(),
    }
    Phase3Collector(
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
    snapshots = load_snapshots_from_jsonl(tmp_path / "phase3_snapshots.jsonl")
    snap = snapshots[0]
    assert snap.market_probability == pytest.approx(0.41)
    assert snap.market_price_observed_at == observed
    assert snap.market_price_observed_at is not None
    assert snap.market_price_observed_at <= snap.decision_ts
    assert snap.price_request_url is not None
    assert "prices-history" in snap.price_request_url
    assert snap.price_raw_path is not None
    assert Path(snap.price_raw_path).is_file()
    assert snap.price_content_sha256
    parquet_rows = read_snapshots_parquet(tmp_path / "phase3_snapshots.parquet")
    restored = row_to_snapshot(parquet_rows[0])
    assert restored.market_price_observed_at == snap.market_price_observed_at
    assert restored.price_request_url == snap.price_request_url
    assert restored.price_raw_path == snap.price_raw_path
    assert restored.price_content_sha256 == snap.price_content_sha256
    assert restored.forecast_hourly == snap.forecast_hourly


def test_archive_hourly_is_not_a_decision_time_observation_feature(tmp_path: Path) -> None:
    in_range = _paris_market(
        condition_id="0xinrange",
        date_label="July 15 2026",
        iso_date="2026-07-15",
        token="yes-in",
    )
    archive_with_pre_decision = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 7200,
        "hourly_units": {"temperature_2m": "°C"},
        "hourly": {
            "time": [
                "2026-07-14T12:00",
                "2026-07-15T00:00",
                "2026-07-15T06:00",
                "2026-07-15T12:00",
            ],
            "temperature_2m": [99.0, 20.0, 88.0, 28.0],
        },
        "daily_units": {"temperature_2m_max": "°C"},
        "daily": {"time": ["2026-07-15"], "temperature_2m_max": [31.2]},
    }
    routes: dict[str, object] = {
        "/public-search": _search_payload([in_range]),
        "/prices-history": {
            "history": [
                {"t": int(datetime(2026, 7, 14, 12, 0, tzinfo=UTC).timestamp()), "p": 0.41},
            ]
        },
        "single-runs-api.open-meteo.com": lambda _p: _load("phase3_single_run_lfpg.json"),
        "archive-api.open-meteo.com": lambda _p: archive_with_pre_decision,
    }
    Phase3Collector(
        http=ReadOnlyHttpClient(transport=RecordingGetTransport(routes), max_retries=0),
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    ).collect(
        Phase3CollectOptions(
            start_date="2026-07-15",
            end_date="2026-07-15",
            output_root=tmp_path,
            max_search_pages=1,
            forecast_lead_hours=6.0,
            cities=("paris",),
            stations_file=STATIONS,
        )
    )
    snap = load_snapshots_from_jsonl(tmp_path / "phase3_snapshots.jsonl")[0]
    assert snap.forecast_lead_hours == 6.0
    assert snap.observation_max_so_far_c is None
    assert snap.observation_as_of is None
    assert snap.diagnostic_actual_max_c == pytest.approx(31.2)
    assert any(
        "not a decision-time" in item.lower() or "point-in-time" in item.lower()
        for item in snap.limitations
    )
    assert all(hour.temperature_c != 99.0 for hour in snap.forecast_hourly)
    assert all(hour.temperature_c != 88.0 for hour in snap.forecast_hourly)


def test_forecast_hourly_is_machine_readable_and_roundtrips(tmp_path: Path) -> None:
    in_range = _paris_market(
        condition_id="0xinrange",
        date_label="July 15 2026",
        iso_date="2026-07-15",
        token="yes-in",
    )
    routes: dict[str, object] = {
        "/public-search": _search_payload([in_range]),
        "/prices-history": {
            "history": [
                {"t": int(datetime(2026, 7, 13, 21, 0, tzinfo=UTC).timestamp()), "p": 0.41},
            ]
        },
        **_weather_routes(),
    }
    Phase3Collector(
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
    snap = load_snapshots_from_jsonl(tmp_path / "phase3_snapshots.jsonl")[0]
    assert snap.forecast_hourly
    temps = [hour.temperature_c for hour in snap.forecast_hourly]
    assert 31.0 in temps
    assert all(hour.valid_time_utc.tzinfo is not None for hour in snap.forecast_hourly)
    first = snap.forecast_hourly[0]
    assert first.dew_point_c is not None
    assert first.humidity_pct is not None
    assert first.cloud_cover_pct is not None
    assert first.wind_speed is not None
    assert first.wind_direction_deg is not None
    assert first.precipitation is not None
    assert first.surface_pressure is not None
    restored = row_to_snapshot(read_snapshots_parquet(tmp_path / "phase3_snapshots.parquet")[0])
    assert restored.forecast_hourly == snap.forecast_hourly
    assert snap.forecast_daily_max_c == pytest.approx(31.0)


PRICE_HISTORY_LOOKBACK_SECONDS = 7 * 86400


def _price_history_query(transport: RecordingGetTransport) -> dict[str, list[str]]:
    urls = [url for _method, url in transport.calls if "/prices-history" in url]
    assert urls
    return parse_qs(urlsplit(urls[0]).query)


def test_price_history_ignores_inconsistent_gamma_dates_and_bounds_to_decision(
    tmp_path: Path,
) -> None:
    # Live Gamma records can have startDate after endDate (sampled Feb 11 Paris).
    in_range = _paris_market(
        condition_id="0xinrange",
        date_label="July 15 2026",
        iso_date="2026-07-15",
        token="yes-in",
    )
    in_range["startDate"] = "2026-07-15T18:22:55Z"
    in_range["endDate"] = "2026-07-15T12:00:00Z"
    observed = datetime(2026, 7, 13, 21, 0, tzinfo=UTC)
    gamma_start_ts = int(datetime(2026, 7, 15, 18, 22, 55, tzinfo=UTC).timestamp())
    gamma_end_ts = int(datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC).timestamp())
    routes: dict[str, object] = {
        "/public-search": _search_payload([in_range]),
        "/prices-history": {"history": [{"t": int(observed.timestamp()), "p": 0.41}]},
        **_weather_routes(),
    }
    transport = RecordingGetTransport(routes)
    Phase3Collector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
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
    snap = load_snapshots_from_jsonl(tmp_path / "phase3_snapshots.jsonl")[0]
    params = _price_history_query(transport)
    start_ts = int(params["startTs"][0])
    end_ts = int(params["endTs"][0])
    decision_ts = int(snap.decision_ts.timestamp())
    assert start_ts != gamma_start_ts
    assert end_ts != gamma_end_ts
    assert start_ts < end_ts
    assert end_ts == decision_ts
    assert start_ts == decision_ts - PRICE_HISTORY_LOOKBACK_SECONDS
    assert end_ts <= decision_ts
    assert snap.market_probability == pytest.approx(0.41)
    assert snap.price_request_url is not None
    assert "prices-history" in snap.price_request_url
    assert f"startTs={start_ts}" in snap.price_request_url
    assert f"endTs={end_ts}" in snap.price_request_url
    assert snap.price_raw_path is not None
    assert Path(snap.price_raw_path).is_file()
    assert snap.price_content_sha256


def test_price_history_http_200_empty_is_not_http_error(tmp_path: Path) -> None:
    in_range = _paris_market(
        condition_id="0xinrange",
        date_label="July 15 2026",
        iso_date="2026-07-15",
        token="yes-in",
    )
    routes: dict[str, object] = {
        "/public-search": _search_payload([in_range]),
        "/prices-history": {"history": []},
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
    assert report.snapshots_written >= 1
    assert report.price_http_errors == 0
    assert report.price_history_empty >= 1
    snap = load_snapshots_from_jsonl(tmp_path / "phase3_snapshots.jsonl")[0]
    assert snap.market_probability is None
    assert snap.market_price_observed_at is None
    assert snap.price_request_url is not None
    assert snap.price_raw_path is not None
    assert Path(snap.price_raw_path).is_file()
    assert snap.price_content_sha256
    limitation_text = " ".join(snap.limitations).lower()
    assert "empty history" in limitation_text
    assert "not an http error" in limitation_text
    manifest = json.loads((tmp_path / "phase3_source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["price_http_errors"] == 0
    assert manifest["price_history_empty"] == report.price_history_empty


def test_price_points_after_decision_are_rejected(tmp_path: Path) -> None:
    in_range = _paris_market(
        condition_id="0xinrange",
        date_label="July 15 2026",
        iso_date="2026-07-15",
        token="yes-in",
    )
    future = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    routes: dict[str, object] = {
        "/public-search": _search_payload([in_range]),
        "/prices-history": {"history": [{"t": int(future.timestamp()), "p": 0.99}]},
        **_weather_routes(),
    }
    transport = RecordingGetTransport(routes)
    Phase3Collector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
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
    snap = load_snapshots_from_jsonl(tmp_path / "phase3_snapshots.jsonl")[0]
    params = _price_history_query(transport)
    end_ts = int(params["endTs"][0])
    decision_ts = int(snap.decision_ts.timestamp())
    assert end_ts == decision_ts
    assert end_ts < int(future.timestamp())
    assert snap.market_probability is None
    assert snap.market_price_observed_at is None
    assert snap.price_request_url is not None
    assert snap.price_raw_path is not None
    assert snap.price_content_sha256
