"""Deterministic city-date-station-checkpoint identity schedule. No network."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from weather_alpha.config.stations import Station, load_stations
from weather_alpha.phase35.checkpoints import decision_timestamp
from weather_alpha.phase35.full_collection.policy import (
    CHECKPOINTS,
    END_DATE,
    START_DATE,
    TARGET_CITIES_CANONICAL,
)
from weather_alpha.research.single_run import choose_ecmwf_run


def inclusive_date_strings(start: str = START_DATE, end: str = END_DATE) -> tuple[str, ...]:
    cursor = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if last < cursor:
        raise ValueError("end date must be on or after start date")
    days: list[str] = []
    while cursor <= last:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return tuple(days)


def city_date_pairs(
    *,
    start: str = START_DATE,
    end: str = END_DATE,
    cities: tuple[str, ...] = TARGET_CITIES_CANONICAL,
) -> tuple[tuple[str, str], ...]:
    return tuple((city, day) for city in cities for day in inclusive_date_strings(start, end))


def catalog_stations(stations_file: Path | None = None) -> tuple[Station, ...]:
    return load_stations(stations_file)


def stations_for_city(city: str, stations: tuple[Station, ...]) -> tuple[Station, ...]:
    key = city.strip().lower()
    matched = tuple(row for row in stations if row.city.strip().lower() == key)
    if not matched:
        raise ValueError(f"station catalog has no station for city {city!r}")
    return matched


def gamma_search_query(city: str, day: str) -> str:
    year, month, date_day = (int(part) for part in day.split("-"))
    month_name = calendar.month_name[month]
    return f"highest temperature in {city} on {month_name} {date_day}, {year}"


def gamma_search_params(city: str, day: str) -> dict[str, Any]:
    return {
        "q": gamma_search_query(city, day),
        "page": 1,
        "limit_per_type": 50,
        "keep_closed_markets": 1,
    }


def gamma_identities(
    *,
    start: str = START_DATE,
    end: str = END_DATE,
    cities: tuple[str, ...] = TARGET_CITIES_CANONICAL,
) -> tuple[str, ...]:
    return tuple(
        f"gamma:{city}:{day}" for city, day in city_date_pairs(start=start, end=end, cities=cities)
    )


def clob_identities(
    *,
    start: str = START_DATE,
    end: str = END_DATE,
    cities: tuple[str, ...] = TARGET_CITIES_CANONICAL,
) -> tuple[str, ...]:
    return tuple(
        f"clob:{city}:{day}" for city, day in city_date_pairs(start=start, end=end, cities=cities)
    )


def ecmwf_logical_identities(
    *,
    start: str = START_DATE,
    end: str = END_DATE,
    cities: tuple[str, ...] = TARGET_CITIES_CANONICAL,
    stations: tuple[Station, ...] | None = None,
) -> tuple[str, ...]:
    catalog = stations if stations is not None else catalog_stations()
    keys: list[str] = []
    seen: set[str] = set()
    for city, day in city_date_pairs(start=start, end=end, cities=cities):
        for station in stations_for_city(city, catalog):
            for lead in CHECKPOINTS:
                decision = decision_timestamp(day, station.timezone_name, lead)
                choice = choose_ecmwf_run(decision_ts=decision, event_date=day, station=station)
                run_param = None if choice is None else choice.run_param
                identity = f"ecmwf:{station.station_id}:{day}:{lead}:{run_param or 'missing'}"
                if identity not in seen:
                    seen.add(identity)
                    keys.append(identity)
    return tuple(keys)


def station_catalog_payload(stations: tuple[Station, ...]) -> list[dict[str, Any]]:
    return [
        {
            "city": station.city,
            "elevation_m": station.elevation_m,
            "iata": station.iata,
            "icao": station.icao,
            "latitude": station.latitude,
            "longitude": station.longitude,
            "name": station.name,
            "station_id": station.station_id,
            "timezone_name": station.timezone_name,
        }
        for station in stations
    ]
