import json
from datetime import UTC, datetime
from pathlib import Path

from weather_alpha.collectors.weather.parser import parse_open_meteo_response

FIXTURES = Path(__file__).parent / "fixtures"


def test_historical_forecast_does_not_invent_issuance_time() -> None:
    payload = json.loads(
        (FIXTURES / "open_meteo_historical_forecast.json").read_text(encoding="utf-8")
    )
    parsed = parse_open_meteo_response(
        payload,
        station_id="LFPG",
        provider="open-meteo-historical-forecast",
        request_url="https://historical-forecast-api.open-meteo.com/v1/forecast",
    )
    assert parsed.forecasts
    assert all(item.issued_at is None for item in parsed.forecasts)
    assert any(
        "stitches" in note.lower() or "issuance" in note.lower() for note in parsed.limitations
    )
    assert parsed.ensemble_members == ()
    daily = [item for item in parsed.forecasts if item.variable == "temperature_2m_max"]
    assert daily and daily[0].temperature_celsius == 27.4


def test_archive_hourly_and_daily_max_use_source_units() -> None:
    payload = json.loads((FIXTURES / "open_meteo_archive.json").read_text(encoding="utf-8"))
    parsed = parse_open_meteo_response(
        payload,
        station_id="LFPG",
        provider="open-meteo-archive",
        request_url="https://archive-api.open-meteo.com/v1/archive",
    )
    assert parsed.hourly_observations
    assert parsed.hourly_observations[0].source_unit == "°C"
    assert parsed.daily_maxima
    assert parsed.daily_maxima[0].local_date == "2024-07-15"
    assert parsed.daily_maxima[0].timezone_name == "Europe/Paris"


def test_ensemble_members_parsed_only_when_present() -> None:
    payload = json.loads((FIXTURES / "open_meteo_ensemble.json").read_text(encoding="utf-8"))
    parsed = parse_open_meteo_response(
        payload,
        station_id="LFPG",
        provider="open-meteo-ensemble",
        request_url="https://ensemble-api.open-meteo.com/v1/ensemble",
    )
    members = {item.member_id for item in parsed.ensemble_members}
    assert members == {"member01", "member02"}
    assert all(item.issued_at is None for item in parsed.ensemble_members)


def test_archive_mixed_units_do_not_treat_humidity_wind_or_pressure_as_temperature() -> None:
    payload = json.loads(
        (FIXTURES / "open_meteo_archive_mixed_units.json").read_text(encoding="utf-8")
    )
    parsed = parse_open_meteo_response(
        payload,
        station_id="LFPG",
        provider="open-meteo-archive",
        request_url="https://archive-api.open-meteo.com/v1/archive",
    )
    by_var = {item.variable: item for item in parsed.hourly_observations}
    required = {
        "temperature_2m",
        "relative_humidity_2m",
        "dew_point_2m",
        "wind_speed_10m",
        "cloud_cover",
        "surface_pressure",
    }
    assert required <= set(by_var)
    assert by_var["temperature_2m"].temperature_celsius == 28.4
    assert by_var["temperature_2m"].source_unit == "°C"
    assert by_var["dew_point_2m"].temperature_celsius == 18.2
    assert by_var["dew_point_2m"].source_unit == "°C"
    assert by_var["relative_humidity_2m"].temperature_celsius is None
    assert by_var["relative_humidity_2m"].source_value == 55.0
    assert by_var["relative_humidity_2m"].source_unit == "%"
    assert by_var["wind_speed_10m"].temperature_celsius is None
    assert by_var["wind_speed_10m"].source_unit == "km/h"
    assert by_var["cloud_cover"].temperature_celsius is None
    assert by_var["cloud_cover"].source_unit == "%"
    assert by_var["surface_pressure"].temperature_celsius is None
    assert by_var["surface_pressure"].source_value == 1013.2
    assert by_var["surface_pressure"].source_unit == "hPa"
    assert parsed.daily_maxima[0].temperature_celsius == 31.2
    assert parsed.daily_maxima[0].source_unit == "°C"


def test_open_meteo_dst_boundary_uses_date_specific_zone_offsets() -> None:
    # Europe/Paris DST started 2024-03-31 02:00. A single utc_offset_seconds of +7200
    # (CEST) would mis-convert the pre-transition local hour.
    payload = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 7200,
        "hourly_units": {"time": "iso8601", "temperature_2m": "°C"},
        "hourly": {
            "time": ["2024-03-30T12:00", "2024-04-01T12:00"],
            "temperature_2m": [12.0, 14.0],
        },
        "daily_units": {"time": "iso8601", "temperature_2m_max": "°C"},
        "daily": {"time": ["2024-03-30", "2024-04-01"], "temperature_2m_max": [13.0, 16.0]},
    }
    parsed = parse_open_meteo_response(
        payload,
        station_id="LFPG",
        provider="open-meteo-archive",
        request_url="https://archive-api.open-meteo.com/v1/archive",
    )
    by_time = {item.observed_at: item for item in parsed.hourly_observations}
    winter = datetime(2024, 3, 30, 12, 0, tzinfo=UTC)
    summer = datetime(2024, 4, 1, 12, 0, tzinfo=UTC)
    winter_utc = datetime(2024, 3, 30, 11, 0, tzinfo=UTC)  # CET UTC+1
    summer_utc = datetime(2024, 4, 1, 10, 0, tzinfo=UTC)  # CEST UTC+2
    assert winter_utc in by_time
    assert summer_utc in by_time
    winter_offset = (winter - winter_utc).total_seconds()
    summer_offset = (summer - summer_utc).total_seconds()
    assert winter_offset == 3600
    assert summer_offset == 7200
    assert winter_offset != summer_offset
    assert [item.local_date for item in parsed.daily_maxima] == ["2024-03-30", "2024-04-01"]


def test_dst_fold_repeated_local_hour_maps_to_distinct_utc_and_persists(tmp_path: Path) -> None:
    from weather_alpha.storage.repository import WeatherAlphaRepository

    payload = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 3600,
        "hourly_units": {"time": "iso8601", "temperature_2m": "°C"},
        "hourly": {
            "time": ["2024-10-27T02:30", "2024-10-27T02:30"],
            "temperature_2m": [11.0, 10.5],
        },
        "daily_units": {"time": "iso8601", "temperature_2m_max": "°C"},
        "daily": {"time": ["2024-10-27"], "temperature_2m_max": [14.0]},
    }
    parsed = parse_open_meteo_response(
        payload,
        station_id="LFPG",
        provider="open-meteo-archive",
        request_url="https://archive-api.open-meteo.com/v1/archive",
        raw_path="/raw/archive.json",
        content_sha256="abc",
    )
    times = [item.observed_at for item in parsed.hourly_observations]
    assert times == [
        datetime(2024, 10, 27, 0, 30, tzinfo=UTC),
        datetime(2024, 10, 27, 1, 30, tzinfo=UTC),
    ]
    assert [item.temperature_celsius for item in parsed.hourly_observations] == [11.0, 10.5]
    assert parsed.daily_maxima[0].local_date == "2024-10-27"
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    for item in parsed.hourly_observations:
        repo.upsert_hourly_observation(item)
    assert repo.count("hourly_observations") == 2
    with repo.connect() as conn:
        stored = [
            row["observed_at"]
            for row in conn.execute(
                "SELECT observed_at FROM hourly_observations ORDER BY observed_at"
            )
        ]
    assert stored == [
        datetime(2024, 10, 27, 0, 30, tzinfo=UTC).isoformat(),
        datetime(2024, 10, 27, 1, 30, tzinfo=UTC).isoformat(),
    ]


def test_dst_fold_extra_duplicate_and_spring_gap_are_not_fabricated() -> None:
    extra = parse_open_meteo_response(
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": 3600,
            "hourly_units": {"time": "iso8601", "temperature_2m": "°C"},
            "hourly": {
                "time": ["2024-10-27T02:30", "2024-10-27T02:30", "2024-10-27T02:30"],
                "temperature_2m": [11.0, 10.5, 10.0],
            },
        },
        station_id="LFPG",
        provider="open-meteo-archive",
        request_url="https://archive-api.open-meteo.com/v1/archive",
    )
    assert [item.observed_at for item in extra.hourly_observations] == [
        datetime(2024, 10, 27, 0, 30, tzinfo=UTC),
        datetime(2024, 10, 27, 1, 30, tzinfo=UTC),
    ]
    assert any("duplicate" in note.lower() or "fold" in note.lower() for note in extra.limitations)

    gap = parse_open_meteo_response(
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": 7200,
            "hourly_units": {"time": "iso8601", "temperature_2m": "°C"},
            "hourly": {
                "time": ["2024-03-31T01:30", "2024-03-31T02:30", "2024-03-31T03:30"],
                "temperature_2m": [8.0, 99.0, 9.0],
            },
        },
        station_id="LFPG",
        provider="open-meteo-archive",
        request_url="https://archive-api.open-meteo.com/v1/archive",
    )
    assert [item.observed_at for item in gap.hourly_observations] == [
        datetime(2024, 3, 31, 0, 30, tzinfo=UTC),
        datetime(2024, 3, 31, 1, 30, tzinfo=UTC),
    ]
    assert [item.temperature_celsius for item in gap.hourly_observations] == [8.0, 9.0]
    assert any("gap" in note.lower() or "skipped" in note.lower() for note in gap.limitations)


def test_archive_provenance_omits_forecast_issuance_limitation() -> None:
    payload = json.loads((FIXTURES / "open_meteo_archive.json").read_text(encoding="utf-8"))
    parsed = parse_open_meteo_response(
        payload,
        station_id="LFPG",
        provider="open-meteo-archive",
        request_url="https://archive-api.open-meteo.com/v1/archive",
    )
    notes = " ".join(parsed.limitations).lower()
    assert "issuance" not in notes
    assert parsed.hourly_observations
    assert all(
        "issuance" not in " ".join(item.provenance.limitations).lower()
        for item in parsed.hourly_observations
    )
    forecast = json.loads(
        (FIXTURES / "open_meteo_historical_forecast.json").read_text(encoding="utf-8")
    )
    forecast_parsed = parse_open_meteo_response(
        forecast,
        station_id="LFPG",
        provider="open-meteo-historical-forecast",
        request_url="https://historical-forecast-api.open-meteo.com/v1/forecast",
    )
    assert any("issuance" in note.lower() for note in forecast_parsed.limitations)
