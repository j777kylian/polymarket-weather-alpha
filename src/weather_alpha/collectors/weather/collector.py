"""Weather collectors using injectable GET transports."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from weather_alpha.collectors.weather.adapters import (
    OpenMeteoArchiveAdapter,
    OpenMeteoEnsembleAdapter,
    OpenMeteoHistoricalForecastAdapter,
    WeatherAdapter,
)
from weather_alpha.collectors.weather.parser import parse_open_meteo_response
from weather_alpha.config.settings import validate_date_range
from weather_alpha.config.stations import Station, load_stations
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyResponse
from weather_alpha.models.timeutil import utc_now
from weather_alpha.storage.raw import PersistedPayload, persist_raw_payload
from weather_alpha.storage.repository import WeatherAlphaRepository


@dataclass(frozen=True, slots=True)
class WeatherCollectOptions:
    start_date: str
    end_date: str
    station_ids: tuple[str, ...] | None = None
    provider: str = "open-meteo-historical-forecast"
    dry_run: bool = False
    stations_file: Path | None = None


@dataclass
class WeatherCollectReport:
    dry_run: bool
    provider: str
    stations: tuple[str, ...]
    start_date: str
    end_date: str
    forecasts_stored: int = 0
    members_stored: int = 0
    hourly_stored: int = 0
    daily_stored: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "provider": self.provider,
            "stations": list(self.stations),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "forecasts_stored": self.forecasts_stored,
            "members_stored": self.members_stored,
            "hourly_stored": self.hourly_stored,
            "daily_stored": self.daily_stored,
            "notes": list(self.notes),
        }


class WeatherCollector:
    def __init__(
        self,
        *,
        http: ReadOnlyHttpClient,
        repository: WeatherAlphaRepository | None,
        raw_root: Path,
        adapter: WeatherAdapter | None = None,
    ) -> None:
        self._http = http
        self._repo = repository
        self._raw_root = raw_root
        self._adapter = adapter

    def collect(self, options: WeatherCollectOptions) -> WeatherCollectReport:
        validate_date_range(options.start_date, options.end_date)
        stations = load_stations(options.stations_file)
        selected = _select_stations(stations, options.station_ids)
        report = WeatherCollectReport(
            dry_run=options.dry_run,
            provider=options.provider,
            stations=tuple(station.station_id for station in selected),
            start_date=options.start_date,
            end_date=options.end_date,
        )
        if options.dry_run:
            report.notes.append("dry-run: no HTTP requests and no writes")
            return report
        if self._repo is None:
            raise RuntimeError("repository is required unless dry-run")
        adapter = self._adapter or _build_adapter(self._http, options.provider)
        for station in selected:
            response = adapter.fetch(
                station, start_date=options.start_date, end_date=options.end_date
            )
            persisted = self._persist(response, source=f"weather/{options.provider}")
            payload = persisted.payload
            if not isinstance(payload, dict):
                report.notes.append(f"non-object payload for {station.station_id}")
                continue
            parsed = parse_open_meteo_response(
                payload,
                station_id=station.station_id,
                provider=options.provider,
                request_url=persisted.request_url,
                retrieved_at=persisted.retrieved_at,
                raw_path=persisted.raw_path,
                content_sha256=persisted.content_sha256,
            )
            report.notes.extend(parsed.limitations)
            for forecast in parsed.forecasts:
                self._repo.upsert_forecast(forecast)
            for member in parsed.ensemble_members:
                self._repo.upsert_ensemble_member(member)
            for observation in parsed.hourly_observations:
                self._repo.upsert_hourly_observation(observation)
            for daily in parsed.daily_maxima:
                self._repo.upsert_daily_maximum(daily)
            report.forecasts_stored += len(parsed.forecasts)
            report.members_stored += len(parsed.ensemble_members)
            report.hourly_stored += len(parsed.hourly_observations)
            report.daily_stored += len(parsed.daily_maxima)
        return report

    def _persist(self, response: ReadOnlyResponse, *, source: str) -> PersistedPayload:
        response.raise_for_status()
        try:
            payload: Any = response.json()
        except ValueError:
            payload = {"raw_text": response.text()}
        retrieved_at = utc_now()
        persisted = persist_raw_payload(
            self._raw_root,
            source=source,
            url=response.url,
            payload=payload,
            retrieved_at=retrieved_at,
        )
        assert self._repo is not None
        self._repo.record_raw_payload(
            content_sha256=persisted.content_sha256,
            source=source,
            request_url=persisted.request_url,
            retrieved_at=persisted.retrieved_at,
            disk_path=persisted.raw_path,
            limitations=(),
        )
        return persisted


def _select_stations(
    stations: tuple[Station, ...], station_ids: tuple[str, ...] | None
) -> tuple[Station, ...]:
    if not station_ids:
        return stations
    wanted = {item.upper() for item in station_ids}
    selected = tuple(station for station in stations if station.station_id.upper() in wanted)
    missing = wanted - {station.station_id.upper() for station in selected}
    if missing:
        raise ValueError(f"unknown station id(s): {sorted(missing)}")
    return selected


def _build_adapter(http: ReadOnlyHttpClient, provider: str) -> WeatherAdapter:
    if provider == "open-meteo-historical-forecast":
        return OpenMeteoHistoricalForecastAdapter(http=http)
    if provider == "open-meteo-archive":
        return OpenMeteoArchiveAdapter(http=http)
    if provider == "open-meteo-ensemble":
        return OpenMeteoEnsembleAdapter(http=http)
    raise ValueError(f"unsupported weather provider: {provider}")
