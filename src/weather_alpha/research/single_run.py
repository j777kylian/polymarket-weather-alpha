"""Open-Meteo Single Runs (ECMWF IFS) with conservative availability lag.

Historical Forecast MUST NOT be used as point-in-time model input.
issued_at = run initialization; available_at = run + 6 hours (conservative).
No snapshot may use a run before available_at.

The Single Runs GET uses models=ecmwf_ifs (plural). start_date/end_date must not
be sent: the endpoint rejects them and returns the whole run horizon. Local-day
filtering is done in parse_single_run_forecast / predicted_daily_max.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from weather_alpha.collectors.weather.parser import _parse_open_meteo_time_series
from weather_alpha.config.stations import Station
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyResponse
from weather_alpha.models.timeutil import ensure_utc

SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
SINGLE_RUN_MODEL = "ecmwf_ifs"
PROVIDER_SINGLE_RUN = "open-meteo-single-run"
ECMWF_IFS_RUN_HOURS: tuple[int, ...] = (0, 6, 12, 18)
CONSERVATIVE_AVAILABILITY_HOURS = 6
ECMWF_IFS_RUN_STEP_HOURS = 6
# Conservative IFS HRES horizon used only to check coverage, not as a skill claim.
CONSERVATIVE_HORIZON_HOURS = 240

SINGLE_RUN_HOURLY_VARIABLES = (
    "temperature_2m",
    "dew_point_2m",
    "relative_humidity_2m",
    "cloud_cover",
    "wind_speed_10m",
    "wind_direction_10m",
    "precipitation",
    "surface_pressure",
)

SINGLE_RUN_LIMITATIONS = (
    "Open-Meteo Single Runs issued_at is the requested run= initialization time.",
    "available_at is conservatively issued_at + 6 hours; public availability is not claimed.",
    "Open-Meteo Historical Forecast is not used as point-in-time model input.",
    "Archive maxima are diagnostic grid/reanalysis values, not Wunderground settlement.",
)


def availability_lag() -> timedelta:
    return timedelta(hours=CONSERVATIVE_AVAILABILITY_HOURS)


@dataclass(frozen=True, slots=True)
class EcmwfRunChoice:
    issued_at: datetime
    available_at: datetime
    run_param: str
    covers_event_local_day: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "issued_at", ensure_utc(self.issued_at))
        object.__setattr__(self, "available_at", ensure_utc(self.available_at))


@dataclass(frozen=True, slots=True)
class HourlyForecastPoint:
    valid_time_utc: datetime
    local_date: str
    temperature_c: float | None
    dew_point_c: float | None
    humidity_pct: float | None
    cloud_cover_pct: float | None
    wind_speed: float | None
    wind_direction_deg: float | None
    precipitation: float | None
    surface_pressure: float | None


@dataclass(frozen=True, slots=True)
class ParsedSingleRun:
    issued_at: datetime
    available_at: datetime
    station_id: str
    hourly: tuple[HourlyForecastPoint, ...]
    request_url: str
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PredictedDailyMax:
    value_c: float
    valid_times: tuple[datetime, ...]
    local_date: str


def choose_ecmwf_run(
    *,
    decision_ts: datetime,
    event_date: str,
    station: Station,
) -> EcmwfRunChoice | None:
    decision = ensure_utc(decision_ts)
    zone = ZoneInfo(station.timezone_name)
    event_end_utc = _event_local_end_utc(event_date, zone)
    candidate = _snap_to_run_hour(decision.replace(minute=0, second=0, microsecond=0))
    step = timedelta(hours=ECMWF_IFS_RUN_STEP_HOURS)
    if candidate > decision:
        candidate = _snap_to_run_hour(candidate - step)
    for _ in range(40):
        candidate = _snap_to_run_hour(candidate)
        available = candidate + availability_lag()
        if available <= decision:
            horizon_end = candidate + timedelta(hours=CONSERVATIVE_HORIZON_HOURS)
            covers = candidate <= event_end_utc <= horizon_end
            if covers:
                return EcmwfRunChoice(
                    issued_at=candidate,
                    available_at=available,
                    run_param=candidate.strftime("%Y-%m-%dT%H:%M"),
                    covers_event_local_day=True,
                )
        candidate -= step
    return None


@dataclass(frozen=True, slots=True)
class OpenMeteoSingleRunAdapter:
    http: ReadOnlyHttpClient
    provider: str = PROVIDER_SINGLE_RUN

    def fetch(
        self,
        station: Station,
        *,
        start_date: str,
        end_date: str,
        run: str,
        extra: Mapping[str, Any] | None = None,
    ) -> ReadOnlyResponse:
        """GET one Single Runs cube.

        start_date/end_date are kept on the call signature so collectors can
        pass the event local date without a wider refactor. They are not sent:
        the endpoint returns the whole run horizon and rejects start_date.
        """
        del start_date, end_date
        params: dict[str, Any] = {
            "latitude": station.latitude,
            "longitude": station.longitude,
            "timezone": station.timezone_name,
            "temperature_unit": "celsius",
            "models": SINGLE_RUN_MODEL,
            "run": run,
            "hourly": ",".join(SINGLE_RUN_HOURLY_VARIABLES),
        }
        if extra:
            params.update(dict(extra))
        params.pop("start_date", None)
        params.pop("end_date", None)
        params.pop("model", None)
        return self.http.get(SINGLE_RUNS_URL, params=params)


def parse_single_run_forecast(
    payload: dict[str, Any],
    *,
    station: Station,
    issued_at: datetime,
    request_url: str,
) -> ParsedSingleRun:
    issued = ensure_utc(issued_at)
    available = issued + availability_lag()
    utc_offset = int(payload.get("utc_offset_seconds") or 0)
    timezone_name = str(payload.get("timezone") or station.timezone_name)
    hourly_raw = payload.get("hourly")
    hourly: dict[str, Any] = hourly_raw if isinstance(hourly_raw, dict) else {}
    times, _notes = _parse_open_meteo_time_series(
        list(hourly.get("time") or []),
        timezone_name=timezone_name,
        utc_offset_seconds=utc_offset,
    )
    zone = ZoneInfo(station.timezone_name)
    points: list[HourlyForecastPoint] = []
    for index, valid_utc in enumerate(times):
        if valid_utc is None:
            continue
        local = valid_utc.astimezone(zone)
        points.append(
            HourlyForecastPoint(
                valid_time_utc=valid_utc,
                local_date=local.date().isoformat(),
                temperature_c=_float_at(hourly.get("temperature_2m"), index),
                dew_point_c=_float_at(hourly.get("dew_point_2m"), index),
                humidity_pct=_float_at(hourly.get("relative_humidity_2m"), index),
                cloud_cover_pct=_float_at(hourly.get("cloud_cover"), index),
                wind_speed=_float_at(hourly.get("wind_speed_10m"), index),
                wind_direction_deg=_float_at(hourly.get("wind_direction_10m"), index),
                precipitation=_float_at(hourly.get("precipitation"), index),
                surface_pressure=_float_at(hourly.get("surface_pressure"), index),
            )
        )
    return ParsedSingleRun(
        issued_at=issued,
        available_at=available,
        station_id=station.station_id,
        hourly=tuple(points),
        request_url=request_url,
        limitations=SINGLE_RUN_LIMITATIONS,
    )


def predicted_daily_max(parsed: ParsedSingleRun, *, event_date: str) -> PredictedDailyMax | None:
    temps: list[tuple[datetime, float]] = []
    for point in parsed.hourly:
        if point.local_date != event_date:
            continue
        if point.temperature_c is None:
            continue
        temps.append((point.valid_time_utc, point.temperature_c))
    if not temps:
        return None
    value = max(item[1] for item in temps)
    valid_times = tuple(item[0] for item in temps if item[1] == value)
    return PredictedDailyMax(value_c=value, valid_times=valid_times, local_date=event_date)


def _snap_to_run_hour(moment: datetime) -> datetime:
    floored = moment.replace(minute=0, second=0, microsecond=0)
    hours_at_or_before = [hour for hour in ECMWF_IFS_RUN_HOURS if hour <= floored.hour]
    if hours_at_or_before:
        return floored.replace(hour=max(hours_at_or_before))
    previous_day = floored - timedelta(days=1)
    return previous_day.replace(hour=max(ECMWF_IFS_RUN_HOURS), minute=0, second=0, microsecond=0)


def _event_local_end_utc(event_date: str, zone: ZoneInfo) -> datetime:
    year, month, day = (int(part) for part in event_date.split("-"))
    local_end = datetime(year, month, day, 23, 59, 59, tzinfo=zone)
    return local_end.astimezone(UTC)


def _float_at(series: Any, index: int) -> float | None:
    if not isinstance(series, list) or index >= len(series):
        return None
    value = series[index]
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
