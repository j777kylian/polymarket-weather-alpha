"""Open-Meteo Single Runs: run+6h availability and local-day max."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from tests.fakes import RecordingGetTransport
from weather_alpha.config.stations import Station
from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.research.single_run import (
    ECMWF_IFS_RUN_HOURS,
    SINGLE_RUN_HOURLY_VARIABLES,
    SINGLE_RUN_MODEL,
    SINGLE_RUNS_URL,
    OpenMeteoSingleRunAdapter,
    availability_lag,
    choose_ecmwf_run,
    parse_single_run_forecast,
    predicted_daily_max,
)


def _lfpg() -> Station:
    return Station(
        station_id="LFPG",
        name="Paris CDG",
        icao="LFPG",
        city="paris",
        latitude=49.0097,
        longitude=2.5479,
        timezone_name="Europe/Paris",
    )


def test_available_at_is_run_plus_six_hours() -> None:
    issued = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    assert availability_lag() == timedelta(hours=6)
    available = issued + availability_lag()
    assert available == datetime(2026, 7, 13, 18, 0, tzinfo=UTC)


def test_ecmwf_run_hours_include_00_06_12_18() -> None:
    assert ECMWF_IFS_RUN_HOURS == (0, 6, 12, 18)


def test_choose_run_requires_available_at_at_or_before_decision() -> None:
    decision = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    event_date = "2026-07-15"
    chosen = choose_ecmwf_run(decision_ts=decision, event_date=event_date, station=_lfpg())
    assert chosen is not None
    assert chosen.available_at <= decision
    assert chosen.issued_at + timedelta(hours=6) == chosen.available_at
    # Forecast horizon must reach the local event day.
    assert chosen.covers_event_local_day is True


def test_freshest_eligible_run_is_chosen() -> None:
    # 12Z becomes available at 18Z; a 12Z decision may use 06Z (available at 12Z), not 00Z.
    decision = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    chosen = choose_ecmwf_run(decision_ts=decision, event_date="2026-07-15", station=_lfpg())
    assert chosen is not None
    assert chosen.issued_at == datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    assert chosen.available_at == datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    assert chosen.run_param == "2026-07-14T06:00"
    assert chosen.covers_event_local_day is True


def test_future_unavailable_runs_are_excluded() -> None:
    # 06Z is issued but not yet available at 11Z; 12Z has not become available either.
    decision = datetime(2026, 7, 14, 11, 0, tzinfo=UTC)
    chosen = choose_ecmwf_run(decision_ts=decision, event_date="2026-07-15", station=_lfpg())
    assert chosen is not None
    assert chosen.available_at <= decision
    assert chosen.issued_at == datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    assert chosen.issued_at != datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    assert chosen.issued_at != datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    assert chosen.issued_at != datetime(2026, 7, 14, 18, 0, tzinfo=UTC)


def test_run_not_used_before_available_at() -> None:
    # 00Z run becomes available at 06Z; a 05Z decision cannot use it.
    # 18Z from the previous UTC day is the freshest eligible run (available at 00Z).
    decision = datetime(2026, 7, 14, 5, 0, tzinfo=UTC)
    chosen = choose_ecmwf_run(decision_ts=decision, event_date="2026-07-15", station=_lfpg())
    assert chosen is not None
    assert chosen.available_at <= decision
    assert chosen.issued_at == datetime(2026, 7, 13, 18, 0, tzinfo=UTC)
    assert chosen.issued_at != datetime(2026, 7, 14, 0, 0, tzinfo=UTC)


def test_predicted_max_uses_only_event_local_calendar_hours() -> None:
    payload = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 7200,
        "hourly": {
            "time": [
                "2026-07-14T22:00",
                "2026-07-15T00:00",
                "2026-07-15T15:00",
                "2026-07-16T00:00",
            ],
            "temperature_2m": [20.0, 25.0, 31.5, 40.0],
        },
        "hourly_units": {"temperature_2m": "°C"},
    }
    parsed = parse_single_run_forecast(
        payload,
        station=_lfpg(),
        issued_at=datetime(2026, 7, 14, 0, 0, tzinfo=UTC),
        request_url=SINGLE_RUNS_URL,
    )
    daily_max = predicted_daily_max(parsed, event_date="2026-07-15")
    assert daily_max is not None
    assert daily_max.value_c == 31.5
    assert daily_max.value_c != 40.0
    assert daily_max.value_c != 20.0


def test_single_run_url_and_model_are_documented() -> None:
    assert SINGLE_RUNS_URL == "https://single-runs-api.open-meteo.com/v1/forecast"
    assert SINGLE_RUN_MODEL == "ecmwf_ifs"


def test_single_run_fetch_sends_exact_params_without_dates() -> None:
    transport = RecordingGetTransport({"/v1/forecast": {"hourly": {"time": []}}})
    adapter = OpenMeteoSingleRunAdapter(http=ReadOnlyHttpClient(transport=transport, max_retries=0))
    station = _lfpg()
    adapter.fetch(
        station,
        start_date="2026-02-09",
        end_date="2026-02-10",
        run="2026-02-09T00:00",
    )
    assert transport.calls
    method, url = transport.calls[0]
    assert method == "GET"
    split = urlsplit(url)
    assert split.scheme == "https"
    assert split.netloc == "single-runs-api.open-meteo.com"
    assert split.path == "/v1/forecast"
    query = parse_qs(split.query, keep_blank_values=True)
    assert query == {
        "latitude": [str(station.latitude)],
        "longitude": [str(station.longitude)],
        "timezone": [station.timezone_name],
        "temperature_unit": ["celsius"],
        "models": [SINGLE_RUN_MODEL],
        "run": ["2026-02-09T00:00"],
        "hourly": [",".join(SINGLE_RUN_HOURLY_VARIABLES)],
    }
    assert "start_date" not in query
    assert "end_date" not in query
    assert "model" not in query
