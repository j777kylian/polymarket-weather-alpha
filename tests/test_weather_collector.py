from pathlib import Path

from tests.fakes import ForbiddenNetworkTransport, RecordingGetTransport
from weather_alpha.collectors.weather.collector import WeatherCollectOptions, WeatherCollector
from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.storage.repository import WeatherAlphaRepository


def test_weather_dry_run_no_network(tmp_path: Path) -> None:
    stations = Path("config/stations.yaml")
    collector = WeatherCollector(
        http=ReadOnlyHttpClient(transport=ForbiddenNetworkTransport()),
        repository=None,
        raw_root=tmp_path / "raw",
    )
    report = collector.collect(
        WeatherCollectOptions(
            start_date="2024-07-01",
            end_date="2024-07-03",
            station_ids=("LFPG",),
            provider="open-meteo-historical-forecast",
            dry_run=True,
            stations_file=stations,
        )
    )
    assert report.dry_run is True
    assert report.stations == ("LFPG",)
    assert not (tmp_path / "raw").exists()


def test_weather_fixture_collection(tmp_path: Path) -> None:
    import json

    payload = json.loads(
        (Path("tests/fixtures/open_meteo_historical_forecast.json")).read_text(encoding="utf-8")
    )
    transport = RecordingGetTransport({"/v1/forecast": payload})
    repo = WeatherAlphaRepository(tmp_path / "db.sqlite")
    repo.init_schema()
    collector = WeatherCollector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
        repository=repo,
        raw_root=tmp_path / "raw",
    )
    report = collector.collect(
        WeatherCollectOptions(
            start_date="2024-07-15",
            end_date="2024-07-15",
            station_ids=("LFPG",),
            provider="open-meteo-historical-forecast",
            stations_file=Path("config/stations.yaml"),
        )
    )
    assert all(method == "GET" for method, _url in transport.calls)
    assert report.forecasts_stored >= 1
    assert repo.count("weather_forecasts") == report.forecasts_stored
    assert repo.count("raw_payloads") == 1
    with repo.connect() as conn:
        row = conn.execute(
            "SELECT raw_path, content_sha256, request_url FROM weather_forecasts"
        ).fetchone()
        assert row["raw_path"]
        assert row["content_sha256"]
        assert Path(row["raw_path"]).is_file()


def test_weather_http_4xx_does_not_persist(tmp_path: Path) -> None:
    from weather_alpha.http.readonly import ReadOnlyHttpError, ReadOnlyResponse

    transport = RecordingGetTransport(
        {
            "/v1/forecast": ReadOnlyResponse(
                status_code=404,
                url="https://historical-forecast-api.open-meteo.com/v1/forecast",
                headers={},
                content=b"{}",
            )
        }
    )
    repo = WeatherAlphaRepository(tmp_path / "db.sqlite")
    repo.init_schema()
    collector = WeatherCollector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
        repository=repo,
        raw_root=tmp_path / "raw",
    )
    try:
        collector.collect(
            WeatherCollectOptions(
                start_date="2024-07-15",
                end_date="2024-07-15",
                station_ids=("LFPG",),
                stations_file=Path("config/stations.yaml"),
            )
        )
    except ReadOnlyHttpError:
        pass
    else:
        raise AssertionError("expected ReadOnlyHttpError for 404")
    assert list((tmp_path / "raw").rglob("*.json")) == []
    assert repo.count("weather_forecasts") == 0


def test_weather_http_5xx_does_not_persist(tmp_path: Path) -> None:
    from weather_alpha.http.readonly import ReadOnlyResponse, RetryExhaustedError

    transport = RecordingGetTransport(
        {
            "/v1/forecast": ReadOnlyResponse(
                status_code=502,
                url="https://historical-forecast-api.open-meteo.com/v1/forecast",
                headers={},
                content=b"{}",
            )
        }
    )
    repo = WeatherAlphaRepository(tmp_path / "db.sqlite")
    repo.init_schema()
    collector = WeatherCollector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
        repository=repo,
        raw_root=tmp_path / "raw",
    )
    try:
        collector.collect(
            WeatherCollectOptions(
                start_date="2024-07-15",
                end_date="2024-07-15",
                station_ids=("LFPG",),
                stations_file=Path("config/stations.yaml"),
            )
        )
    except RetryExhaustedError:
        pass
    else:
        raise AssertionError("expected RetryExhaustedError for 502")
    assert list((tmp_path / "raw").rglob("*.json")) == []
