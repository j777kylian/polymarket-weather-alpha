"""Phase 3 market/outcome mapping: Fahrenheit, Will-form, settlement labels."""

from __future__ import annotations

from weather_alpha.collectors.polymarket.parser import (
    parse_gamma_market,
    parse_temperature_bucket,
)
from weather_alpha.research.settlement import parse_settlement_label


def test_fahrenheit_exact_bucket_converts_to_celsius_retaining_raw_label() -> None:
    bucket = parse_temperature_bucket("86°F")
    assert bucket is not None
    assert bucket.kind == "exact"
    assert bucket.label == "86°F"
    assert bucket.min_c == pytest_approx_c(fahrenheit_to_c(86.0))
    assert bucket.max_c == bucket.min_c


def test_fahrenheit_range_and_tail_buckets() -> None:
    ranged = parse_temperature_bucket("80-84°F")
    assert ranged is not None
    assert ranged.kind == "range"
    assert ranged.label == "80-84°F"
    assert ranged.min_c == pytest_approx_c(fahrenheit_to_c(80.0))
    assert ranged.max_c == pytest_approx_c(fahrenheit_to_c(84.0))

    below = parse_temperature_bucket("79°F or below")
    assert below is not None
    assert below.kind == "below"
    assert below.max_c == pytest_approx_c(fahrenheit_to_c(79.0))
    assert below.min_c is None

    above = parse_temperature_bucket("90°F or higher")
    assert above is not None
    assert above.kind == "above"
    assert above.min_c == pytest_approx_c(fahrenheit_to_c(90.0))
    assert above.max_c is None


def test_will_form_question_parses_city_and_date() -> None:
    payload = {
        "id": "ny-1",
        "question": "Will the highest temperature in New York City be 86°F on July 15, 2026?",
        "conditionId": "0x" + "11" * 32,
        "slug": "will-the-highest-temperature-in-new-york-city-be-86f-on-july-15-2026",
        "description": (
            "Resolves to the highest temperature at LaGuardia Airport (KLGA). "
            "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA."
        ),
        "groupItemTitle": "86°F",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "closed": True,
        "active": False,
        "outcomePrices": '["1", "0"]',
    }
    parsed = parse_gamma_market(payload, retrieved_url="https://gamma-api.polymarket.com/markets")
    assert parsed.market.city == "new york"
    assert parsed.market.event_date == "2026-07-15"
    assert parsed.market.station_icao == "KLGA"
    assert parsed.market.parse_status == "resolved"
    yes = next(o for o in parsed.outcomes if o.outcome_label.lower() == "yes")
    assert yes.bucket_kind == "exact"
    assert yes.group_item_title == "86°F"
    assert yes.temperature_celsius_min == pytest_approx_c(fahrenheit_to_c(86.0))


def test_wunderground_url_trailing_period_extracts_klga_not_english_tokens() -> None:
    payload = {
        "id": "ny-live",
        "question": "Will the highest temperature in New York City be 86°F on July 15, 2026?",
        "conditionId": "0x" + "22" * 32,
        "slug": "will-the-highest-temperature-in-new-york-city-be-86f-on-july-15-2026",
        "description": (
            "This market will resolve to the highest temperature recorded at the "
            "LaGuardia Airport Station in degrees Fahrenheit on July 15, 2026. "
            "THIS WILL resolve from the CITY print. Resolution source: "
            "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA."
        ),
        "groupItemTitle": "86°F",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
    }
    parsed = parse_gamma_market(payload, retrieved_url="https://gamma-api.polymarket.com/markets")
    assert parsed.market.city == "new york"
    assert parsed.market.event_date == "2026-07-15"
    assert parsed.market.station_icao == "KLGA"


def test_wunderground_url_trailing_period_extracts_eglc_london() -> None:
    payload = {
        "id": "lon-live",
        "question": "Highest temperature in London on February 15, 2026?",
        "conditionId": "0x" + "33" * 32,
        "slug": "highest-temperature-in-london-on-february-15-2026",
        "description": (
            "This market will resolve to the temperature range that contains the "
            "highest temperature recorded at the London City Airport Station in "
            "degrees Celsius on 15 Feb '26. THIS CITY AIRPORT print is authoritative. "
            "Resolution source: https://www.wunderground.com/history/daily/gb/london/EGLC."
        ),
        "groupItemTitle": "12°C",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-lon", "no-lon"]',
    }
    parsed = parse_gamma_market(payload, retrieved_url="https://gamma-api.polymarket.com/markets")
    assert parsed.market.city == "london"
    assert parsed.market.event_date == "2026-02-15"
    assert parsed.market.station_icao == "EGLC"
    assert parsed.market.station_icao != "CITY"


def test_icao_url_does_not_accept_arbitrary_english_tokens_without_url_segment() -> None:
    payload = {
        "id": "no-icao",
        "question": "Highest temperature in London on February 15, 2026?",
        "conditionId": "0x" + "44" * 32,
        "slug": "highest-temperature-in-london-on-february-15-2026",
        "description": "London City Airport Station THIS WILL be used. No URL is present.",
        "groupItemTitle": "12°C",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-lon", "no-lon"]',
    }
    parsed = parse_gamma_market(payload, retrieved_url="https://gamma-api.polymarket.com/markets")
    assert parsed.market.station_icao is None
    assert any("station" in note.lower() for note in parsed.market.parse_notes)


def test_settlement_label_yes_when_closed_and_binary_10() -> None:
    result = parse_settlement_label(
        closed=True,
        resolved=True,
        outcomes=["Yes", "No"],
        outcome_prices=["1", "0"],
    )
    assert result.label == "Yes"
    assert result.quarantine_reason is None


def test_settlement_label_no_when_closed_and_binary_01() -> None:
    result = parse_settlement_label(
        closed=True,
        resolved=True,
        outcomes=["Yes", "No"],
        outcome_prices=[0, 1],
    )
    assert result.label == "No"
    assert result.quarantine_reason is None


def test_settlement_quarantines_ambiguous_or_open_markets() -> None:
    open_m = parse_settlement_label(
        closed=False,
        resolved=False,
        outcomes=["Yes", "No"],
        outcome_prices=["1", "0"],
    )
    assert open_m.label is None
    assert open_m.quarantine_reason is not None

    ambig = parse_settlement_label(
        closed=True,
        resolved=True,
        outcomes=["Yes", "No"],
        outcome_prices=["0.7", "0.3"],
    )
    assert ambig.label is None
    assert ambig.quarantine_reason is not None


def fahrenheit_to_c(value: float) -> float:
    return (value - 32.0) * 5.0 / 9.0


def pytest_approx_c(value: float) -> float:
    # Exact float compare for formula-based conversion used by production code.
    return value
