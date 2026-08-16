"""Station catalog loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REQUIRED_STATION_IDS = frozenset({"LFPG", "EGLC", "EDDM", "EHAM", "KJFK", "LIMC", "KLGA"})


@dataclass(frozen=True, slots=True)
class Station:
    station_id: str
    name: str
    icao: str
    city: str
    latitude: float
    longitude: float
    timezone_name: str
    iata: str | None = None
    elevation_m: float | None = None
    coordinate_source: str | None = None


def default_stations_path() -> Path:
    repo_config = Path.cwd() / "config" / "stations.yaml"
    if repo_config.is_file():
        return repo_config
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "stations.yaml"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("config/stations.yaml not found; pass --stations-file explicitly")


def load_stations(path: Path | None = None) -> tuple[Station, ...]:
    target = path or default_stations_path()
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "stations" not in raw:
        raise ValueError(f"invalid stations file: {target}")
    stations = tuple(_parse_station(item) for item in raw["stations"])
    ids = {station.station_id for station in stations}
    missing = REQUIRED_STATION_IDS - ids
    if missing:
        raise ValueError(f"stations file missing required ICAO ids: {sorted(missing)}")
    return stations


def _parse_station(item: Any) -> Station:
    if not isinstance(item, dict):
        raise ValueError("station entry must be a mapping")
    elevation = item.get("elevation_m")
    elevation_m = None if elevation is None or elevation == "" else float(str(elevation))
    return Station(
        station_id=str(item["station_id"]),
        name=str(item["name"]),
        icao=str(item.get("icao") or item["station_id"]),
        city=str(item["city"]),
        latitude=float(item["latitude"]),
        longitude=float(item["longitude"]),
        timezone_name=str(item["timezone_name"]),
        iata=_opt(item.get("iata")),
        elevation_m=elevation_m,
        coordinate_source=_opt(item.get("coordinate_source")),
    )


def _opt(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
