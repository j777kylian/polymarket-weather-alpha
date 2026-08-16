"""Phase 3 station resolution. Source ICAO is never remapped."""

from __future__ import annotations

from weather_alpha.config.stations import Station, load_stations

PHASE3_REQUIRED_STATION_IDS = frozenset({"LFPG", "EGLC", "EDDM", "EHAM", "LIMC", "KLGA"})


def resolve_research_station(
    icao: str | None,
    stations: tuple[Station, ...] | None = None,
) -> tuple[Station | None, str | None]:
    if icao is None or not icao.strip():
        return None, "station ICAO missing; unknown station quarantined"
    token = icao.strip().upper()
    catalog = stations if stations is not None else load_stations()
    by_id = {station.station_id.upper(): station for station in catalog}
    if token in by_id:
        return by_id[token], None
    reason = f"unknown station {token}; not remapped (e.g. LFPB is not LFPG)"
    return None, reason
