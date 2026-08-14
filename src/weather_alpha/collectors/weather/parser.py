"""Open-Meteo JSON parsers.

generationtime_ms is API processing latency, not a forecast issuance time.
The Historical Forecast API stitches successive model runs into a continuous
series and does not expose per-timestep run initialization time. Ensemble
member issuance times are likewise not provided. Those fields are stored as
null with explicit provenance limitations. Members are parsed only when the
response includes `*_memberNN` series.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from weather_alpha.models.records import (
    DailyActualMaximum,
    ForecastEnsembleMember,
    HourlyObservation,
    Provenance,
    WeatherForecast,
)
from weather_alpha.models.timeutil import parse_timestamp, utc_now
from weather_alpha.models.units import UnitError, normalize_temperature

_MEMBER_RE = re.compile(r"^(?P<var>.+)_member(?P<member>\d+)$")

HISTORICAL_FORECAST_LIMITATIONS = (
    "Open-Meteo Historical Forecast API stitches the first hours of successive "
    "model runs into a continuous series; per-timestep forecast issuance/run "
    "time is not provided and is stored as null.",
    "generationtime_ms is API processing latency, not model initialization time.",
)
ENSEMBLE_LIMITATIONS = (
    "Open-Meteo Ensemble API does not provide per-member issuance/run timestamps; "
    "issued_at is stored as null.",
    "Ensemble members are stored only when the JSON includes *_memberNN series.",
)
ARCHIVE_LIMITATIONS = (
    "Open-Meteo Archive /v1/archive is reanalysis-based (ERA5 family), not METAR.",
    "Daily maxima use the timezone supplied in the API response.",
)


@dataclass(frozen=True, slots=True)
class ParsedWeather:
    forecasts: tuple[WeatherForecast, ...]
    ensemble_members: tuple[ForecastEnsembleMember, ...]
    hourly_observations: tuple[HourlyObservation, ...]
    daily_maxima: tuple[DailyActualMaximum, ...]
    limitations: tuple[str, ...]


def parse_open_meteo_response(
    payload: dict[str, Any],
    *,
    station_id: str,
    provider: str,
    request_url: str,
    model: str | None = None,
    issued_at: datetime | None = None,
    retrieved_at: datetime | None = None,
    raw_path: str | None = None,
    content_sha256: str | None = None,
) -> ParsedWeather:
    limitations = _limitations_for(provider)
    if issued_at is None and "archive" not in provider:
        limitations = (
            *limitations,
            "forecast issuance timestamp unavailable in this response; not fabricated",
        )
    utc_offset = int(payload.get("utc_offset_seconds") or 0)
    timezone_name = str(payload.get("timezone") or "GMT")
    payload_timezone = payload.get("timezone")
    hourly_raw = payload.get("hourly")
    daily_raw = payload.get("daily")
    hourly_units_raw = payload.get("hourly_units")
    daily_units_raw = payload.get("daily_units")
    hourly: dict[str, Any] = hourly_raw if isinstance(hourly_raw, dict) else {}
    daily: dict[str, Any] = daily_raw if isinstance(daily_raw, dict) else {}
    hourly_units: dict[str, Any] = hourly_units_raw if isinstance(hourly_units_raw, dict) else {}
    daily_units: dict[str, Any] = daily_units_raw if isinstance(daily_units_raw, dict) else {}

    times, time_notes = _parse_open_meteo_time_series(
        list(hourly.get("time") or []),
        timezone_name=payload_timezone,
        utc_offset_seconds=utc_offset,
    )
    limitations = (*limitations, *time_notes)
    provenance = Provenance(
        source=provider,
        retrieved_at=retrieved_at or utc_now(),
        request_url=request_url,
        raw_path=raw_path,
        content_sha256=content_sha256,
        limitations=limitations,
    )
    forecasts: list[WeatherForecast] = []
    members: list[ForecastEnsembleMember] = []
    hourly_obs: list[HourlyObservation] = []
    for key, series in hourly.items():
        if key == "time" or not isinstance(series, list):
            continue
        unit = hourly_units.get(key)
        unit_text = None if unit is None else str(unit)
        member_match = _MEMBER_RE.match(key)
        for index, valid_time in enumerate(times):
            if valid_time is None:
                continue
            raw_value = series[index] if index < len(series) else None
            source_value, source_unit, celsius = _quantity_fields(raw_value, unit_text, key)
            if member_match:
                members.append(
                    ForecastEnsembleMember(
                        station_id=station_id,
                        provider=provider,
                        valid_time=valid_time,
                        member_id=f"member{member_match.group('member')}",
                        variable=member_match.group("var"),
                        provenance=provenance,
                        model=model,
                        issued_at=issued_at,
                        temperature_celsius=celsius,
                        source_value=source_value,
                        source_unit=source_unit,
                    )
                )
                continue
            if provider.endswith("archive") or provider == "open-meteo-archive":
                hourly_obs.append(
                    HourlyObservation(
                        station_id=station_id,
                        provider=provider,
                        observed_at=valid_time,
                        variable=key,
                        provenance=provenance,
                        temperature_celsius=celsius,
                        source_value=source_value,
                        source_unit=source_unit,
                    )
                )
            else:
                forecasts.append(
                    WeatherForecast(
                        station_id=station_id,
                        provider=provider,
                        valid_time=valid_time,
                        variable=key,
                        provenance=provenance,
                        model=model,
                        issued_at=issued_at,
                        temperature_celsius=celsius,
                        source_value=source_value,
                        source_unit=source_unit,
                    )
                )

    daily_maxima: list[DailyActualMaximum] = []
    daily_times = [str(value) for value in daily.get("time") or []]
    daily_max_series = daily.get("temperature_2m_max")
    daily_unit_raw = daily_units.get("temperature_2m_max")
    daily_unit = None if daily_unit_raw is None else str(daily_unit_raw)
    if isinstance(daily_max_series, list):
        for index, local_date in enumerate(daily_times):
            raw_value = daily_max_series[index] if index < len(daily_max_series) else None
            source_value, source_unit, celsius = _quantity_fields(
                raw_value, daily_unit, "temperature_2m_max"
            )
            if provider.endswith("archive") or provider == "open-meteo-archive":
                daily_maxima.append(
                    DailyActualMaximum(
                        station_id=station_id,
                        provider=provider,
                        local_date=local_date[:10],
                        timezone_name=timezone_name,
                        provenance=provenance,
                        temperature_celsius=celsius,
                        source_value=source_value,
                        source_unit=source_unit,
                    )
                )
            else:
                valid = _parse_open_meteo_time(
                    f"{local_date[:10]}T00:00",
                    timezone_name=payload_timezone,
                    utc_offset_seconds=utc_offset,
                )
                forecasts.append(
                    WeatherForecast(
                        station_id=station_id,
                        provider=provider,
                        valid_time=valid,
                        variable="temperature_2m_max",
                        provenance=provenance,
                        model=model,
                        issued_at=issued_at,
                        temperature_celsius=celsius,
                        source_value=source_value,
                        source_unit=source_unit,
                    )
                )

    return ParsedWeather(
        forecasts=tuple(forecasts),
        ensemble_members=tuple(members),
        hourly_observations=tuple(hourly_obs),
        daily_maxima=tuple(daily_maxima),
        limitations=limitations,
    )


def _quantity_fields(
    raw_value: Any, unit: str | None, variable: str
) -> tuple[float | None, str | None, float | None]:
    if raw_value is None:
        return None, unit, None
    source_value = float(raw_value)
    source_unit = unit
    if not _is_temperature_variable(variable) or unit is None:
        return source_value, source_unit, None
    try:
        temp = normalize_temperature(source_value, unit)
    except UnitError:
        return source_value, source_unit, None
    return temp.source_value, source_unit, temp.celsius


def _is_temperature_variable(name: str) -> bool:
    key = name.lower()
    return "temperature" in key or "dew_point" in key or "dewpoint" in key


def _limitations_for(provider: str) -> tuple[str, ...]:
    if "ensemble" in provider:
        return ENSEMBLE_LIMITATIONS
    if "archive" in provider:
        return ARCHIVE_LIMITATIONS
    return HISTORICAL_FORECAST_LIMITATIONS


def _parse_open_meteo_time_series(
    values: list[Any],
    *,
    timezone_name: Any,
    utc_offset_seconds: int,
) -> tuple[list[datetime | None], tuple[str, ...]]:
    zone = _zoneinfo_or_none(timezone_name)
    notes: list[str] = []
    occurrence_counts: dict[tuple[int, int, int, int, int, int], int] = {}
    times: list[datetime | None] = []
    for value in values:
        parsed, note = _localize_series_value(
            value,
            zone=zone,
            utc_offset_seconds=utc_offset_seconds,
            occurrence_counts=occurrence_counts,
        )
        times.append(parsed)
        if note:
            notes.append(note)
    return times, tuple(notes)


def _parse_open_meteo_time(
    value: Any,
    *,
    timezone_name: Any,
    utc_offset_seconds: int,
) -> datetime:
    parsed, _note = _localize_series_value(
        value,
        zone=_zoneinfo_or_none(timezone_name),
        utc_offset_seconds=utc_offset_seconds,
        occurrence_counts={},
    )
    if parsed is None:
        offset = timezone(timedelta(seconds=utc_offset_seconds))
        naive = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if naive.tzinfo is not None:
            return naive.astimezone(UTC)
        return naive.replace(tzinfo=offset).astimezone(UTC)
    return parsed


def _localize_series_value(
    value: Any,
    *,
    zone: ZoneInfo | None,
    utc_offset_seconds: int,
    occurrence_counts: dict[tuple[int, int, int, int, int, int], int],
) -> tuple[datetime | None, str | None]:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC), None
        naive = datetime(
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
        )
    else:
        text = str(value)
        if text.endswith("Z") or "+" in text[10:] or text.endswith("UTC"):
            return parse_timestamp(text), None
        parsed_local = datetime.fromisoformat(text)
        if parsed_local.tzinfo is not None:
            return parsed_local.astimezone(UTC), None
        naive = parsed_local
    if zone is None:
        offset = timezone(timedelta(seconds=utc_offset_seconds))
        return naive.replace(tzinfo=offset).astimezone(UTC), None
    kind = _dst_kind(naive, zone)
    key = (naive.year, naive.month, naive.day, naive.hour, naive.minute, naive.second)
    wall = naive.isoformat(timespec="minutes")
    if kind == "gap":
        return None, (
            f"skipped nonexistent local timestamp {wall} during DST spring gap; "
            "original value retained only in the raw payload"
        )
    seen = occurrence_counts.get(key, 0)
    if kind == "ambiguous":
        if seen >= 2:
            return None, (
                f"skipped extra duplicate ambiguous local timestamp {wall}; "
                "not fabricated beyond fold=0 then fold=1"
            )
        localized = naive.replace(tzinfo=zone, fold=seen).astimezone(UTC)
        occurrence_counts[key] = seen + 1
        return localized, None
    if seen >= 1:
        return None, (
            f"skipped extra duplicate local timestamp {wall}; original value retained only in the raw payload"
        )
    occurrence_counts[key] = 1
    return naive.replace(tzinfo=zone).astimezone(UTC), None


def _dst_kind(naive: datetime, zone: ZoneInfo) -> str:
    first = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
    second = naive.replace(tzinfo=zone, fold=1).astimezone(UTC)
    if first == second:
        return "unambiguous"
    if first < second:
        return "ambiguous"
    return "gap"


def _zoneinfo_or_none(name: Any) -> ZoneInfo | None:
    if name is None:
        return None
    text = str(name).strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None
