"""Open-Meteo GET adapters. No API keys are required for the public endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from weather_alpha.config.stations import Station
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyResponse

HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

PROVIDER_HISTORICAL_FORECAST = "open-meteo-historical-forecast"
PROVIDER_ARCHIVE = "open-meteo-archive"
PROVIDER_ENSEMBLE = "open-meteo-ensemble"

ARCHIVE_HOURLY_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "wind_speed_10m",
    "cloud_cover",
    "surface_pressure",
)


class WeatherAdapter(Protocol):
    def fetch(
        self,
        station: Station,
        *,
        start_date: str,
        end_date: str,
        extra: Mapping[str, Any] | None = None,
    ) -> ReadOnlyResponse: ...


@dataclass(frozen=True, slots=True)
class OpenMeteoHistoricalForecastAdapter:
    http: ReadOnlyHttpClient
    provider: str = PROVIDER_HISTORICAL_FORECAST

    def fetch(
        self,
        station: Station,
        *,
        start_date: str,
        end_date: str,
        extra: Mapping[str, Any] | None = None,
    ) -> ReadOnlyResponse:
        params = _base_params(station, start_date, end_date)
        params["hourly"] = "temperature_2m"
        params["daily"] = "temperature_2m_max"
        if extra:
            params.update(dict(extra))
        return self.http.get(HISTORICAL_FORECAST_URL, params=params)


@dataclass(frozen=True, slots=True)
class OpenMeteoArchiveAdapter:
    http: ReadOnlyHttpClient
    provider: str = PROVIDER_ARCHIVE

    def fetch(
        self,
        station: Station,
        *,
        start_date: str,
        end_date: str,
        extra: Mapping[str, Any] | None = None,
    ) -> ReadOnlyResponse:
        params = _base_params(station, start_date, end_date)
        params["hourly"] = ",".join(ARCHIVE_HOURLY_VARIABLES)
        params["daily"] = "temperature_2m_max"
        if extra:
            params.update(dict(extra))
        return self.http.get(ARCHIVE_URL, params=params)


@dataclass(frozen=True, slots=True)
class OpenMeteoEnsembleAdapter:
    http: ReadOnlyHttpClient
    provider: str = PROVIDER_ENSEMBLE

    def fetch(
        self,
        station: Station,
        *,
        start_date: str,
        end_date: str,
        extra: Mapping[str, Any] | None = None,
    ) -> ReadOnlyResponse:
        params = _base_params(station, start_date, end_date)
        params["hourly"] = "temperature_2m"
        params["models"] = "ecmwf_ifs025"
        if extra:
            params.update(dict(extra))
        return self.http.get(ENSEMBLE_URL, params=params)


ADAPTERS = {
    PROVIDER_HISTORICAL_FORECAST: OpenMeteoHistoricalForecastAdapter,
    PROVIDER_ARCHIVE: OpenMeteoArchiveAdapter,
    PROVIDER_ENSEMBLE: OpenMeteoEnsembleAdapter,
}


def _base_params(station: Station, start_date: str, end_date: str) -> dict[str, Any]:
    return {
        "latitude": station.latitude,
        "longitude": station.longitude,
        "start_date": start_date,
        "end_date": end_date,
        "timezone": station.timezone_name,
        "temperature_unit": "celsius",
    }
