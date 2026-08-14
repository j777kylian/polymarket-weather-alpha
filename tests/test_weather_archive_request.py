from urllib.parse import parse_qs, urlsplit

from tests.fakes import RecordingGetTransport
from weather_alpha.collectors.weather.adapters import (
    ARCHIVE_HOURLY_VARIABLES,
    OpenMeteoArchiveAdapter,
)
from weather_alpha.config.stations import load_stations
from weather_alpha.http.readonly import ReadOnlyHttpClient


def test_archive_adapter_requests_required_hourly_and_daily_variables() -> None:
    transport = RecordingGetTransport({"/v1/archive": {"hourly": {"time": []}}})
    adapter = OpenMeteoArchiveAdapter(http=ReadOnlyHttpClient(transport=transport, max_retries=0))
    station = next(s for s in load_stations() if s.station_id == "LFPG")
    adapter.fetch(station, start_date="2024-07-15", end_date="2024-07-15")
    assert transport.calls
    _method, url = transport.calls[0]
    query = parse_qs(urlsplit(url).query)
    hourly = query["hourly"][0].split(",")
    assert hourly == list(ARCHIVE_HOURLY_VARIABLES)
    assert query["daily"] == ["temperature_2m_max"]
