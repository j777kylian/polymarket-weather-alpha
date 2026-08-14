from datetime import UTC, datetime, timezone

import pytest

from weather_alpha.models.timeutil import ensure_utc, parse_timestamp
from weather_alpha.models.units import Temperature, fahrenheit_to_celsius, normalize_temperature


def test_naive_datetime_is_rejected() -> None:
    naive = datetime(2024, 7, 15, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone"):
        ensure_utc(naive)


def test_aware_non_utc_is_converted() -> None:
    from datetime import timedelta

    offset_tz = timezone(timedelta(hours=-4))
    local = datetime(2024, 7, 15, 8, 0, 0, tzinfo=offset_tz)
    utc = ensure_utc(local)
    assert utc.tzinfo == UTC
    assert utc == datetime(2024, 7, 15, 12, 0, 0, tzinfo=UTC)


def test_unix_seconds_parse_to_utc() -> None:
    parsed = parse_timestamp(1_720_000_000)
    assert parsed.tzinfo == UTC
    assert parsed == datetime.fromtimestamp(1_720_000_000, tz=UTC)


def test_unix_milliseconds_auto_detected_and_normalized_to_utc() -> None:
    seconds = parse_timestamp(1_720_000_000)
    millis = parse_timestamp(1_720_000_000_000)
    millis_str = parse_timestamp("1720000000000")
    assert millis == seconds
    assert millis_str == seconds
    assert millis.tzinfo == UTC


def test_iso_z_parse_to_utc() -> None:
    parsed = parse_timestamp("2024-07-15T16:00:00Z")
    assert parsed == datetime(2024, 7, 15, 16, 0, 0, tzinfo=UTC)


def test_fahrenheit_normalized_to_celsius_retains_source() -> None:
    temp = normalize_temperature(86, "F")
    assert temp == Temperature(
        source_value=86.0,
        source_unit="F",
        celsius=30.0,
    )
    assert fahrenheit_to_celsius(32) == 0.0


def test_celsius_passthrough_retains_source() -> None:
    temp = normalize_temperature(21.5, "°C")
    assert temp.source_value == 21.5
    assert temp.source_unit == "°C"
    assert temp.celsius == 21.5


def test_normalize_temperature_preserves_exact_api_source_unit_string() -> None:
    celsius = normalize_temperature(21.5, "°C")
    assert celsius.source_unit == "°C"
    assert celsius.celsius == 21.5
    fahrenheit = normalize_temperature(86, "°F")
    assert fahrenheit.source_unit == "°F"
    assert fahrenheit.celsius == 30.0
    kelvin = normalize_temperature(273.15, "K")
    assert kelvin.source_unit == "K"
    assert kelvin.celsius == 0.0
