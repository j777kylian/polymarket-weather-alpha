from pathlib import Path

from weather_alpha.config.stations import REQUIRED_STATION_IDS, load_stations


def test_station_config_covers_required_airports() -> None:
    stations = load_stations()
    ids = {station.station_id for station in stations}
    assert ids == REQUIRED_STATION_IDS
    assert {"LFPG", "EGLC", "EDDM", "EHAM", "KJFK", "LIMC"} == REQUIRED_STATION_IDS
    for station in stations:
        assert station.latitude is not None
        assert station.longitude is not None
        assert station.timezone_name
        assert -90 <= station.latitude <= 90
        assert -180 <= station.longitude <= 180


def test_station_config_file_exists() -> None:
    path = Path("config/stations.yaml")
    assert path.is_file()
