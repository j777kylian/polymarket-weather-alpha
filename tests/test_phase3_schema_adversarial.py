"""Blocker 3 adversarial: HTTP-200 malformed schema is ProviderSchemaError + quarantine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from weather_alpha.research.prices import parse_price_history_points
from weather_alpha.research.provider_schema import (
    ProviderSchemaError,
    validate_archive_payload,
    validate_gamma_search_payload,
    validate_prices_history_payload,
    validate_single_run_payload,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "payload",
    [
        {"history": [{"t": 1, "p": None}]},
        {"history": [{"t": 1}]},
        {"history": [{"t": 1, "p": True}]},
        {"history": [{"t": 1, "p": False}]},
        {"history": [{"t": 1, "p": {"x": 1}}]},
        {"history": [{"t": 1, "p": [0.1]}]},
        {"history": [{"t": 1, "p": "nope"}]},
        {"history": [{"t": 1, "p": 1.5}]},
        {"history": [{"t": 1, "p": -0.01}]},
        {"history": [{"t": 1, "p": float("nan")}]},
        {"history": [{"t": None, "p": 0.2}]},
        {"history": [{"t": True, "p": 0.2}]},
    ],
)
def test_clob_history_requires_valid_t_and_p(payload: object) -> None:
    result = validate_prices_history_payload(payload)
    assert result.status == "malformed"
    with pytest.raises(ProviderSchemaError):
        result.raise_for_status()
    with pytest.raises(ProviderSchemaError):
        parse_price_history_points(payload)


def test_clob_numeric_string_p_and_empty_history_ok() -> None:
    ok = {"history": [{"t": 1_720_000_000, "p": "0.41"}]}
    assert validate_prices_history_payload(ok).status == "ok"
    points = parse_price_history_points(ok)
    assert len(points) == 1
    assert points[0].price == pytest.approx(0.41)
    assert validate_prices_history_payload({"history": []}).status == "empty"
    assert parse_price_history_points({"history": []}) == ()


def test_clob_unknown_unused_fields_allowed() -> None:
    payload = {
        "history": [{"t": 10, "p": 0.2, "extra": "ok"}],
        "unused_top": 1,
    }
    assert validate_prices_history_payload(payload).status == "ok"


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "nope"},
        {"events": "x"},
        {"markets": {}},
        {"unexpected": True},
        {"events": [{"id": "", "markets": []}], "markets": []},
        {"events": [{"markets": []}], "markets": []},
        {"events": [{"id": "1", "markets": "bad"}], "markets": []},
        {"events": [{"id": "1", "markets": [None]}], "markets": []},
        {"events": [{"id": "1", "markets": ["scalar"]}], "markets": []},
        {"events": [{"id": "1", "markets": [{"question": "x"}]}], "markets": []},
        {"events": [], "markets": [None]},
        {"events": [], "markets": ["scalar"]},
        {"events": [], "markets": [{"question": "only"}]},
    ],
)
def test_gamma_malformed_nested_identity_fails(payload: object) -> None:
    result = validate_gamma_search_payload(payload)
    assert result.status in {"malformed", "source_drift"}
    with pytest.raises(ProviderSchemaError):
        result.raise_for_status()


def test_gamma_valid_parent_and_market_identity() -> None:
    payload = {
        "events": [
            {
                "id": "279170",
                "slug": "highest-temperature-in-nyc-on-march-21-2026",
                "markets": [
                    {
                        "conditionId": "0xabc",
                        "question": "Will the highest temperature in NYC be 52-53°F?",
                        "slug": "highest-temperature-in-nyc-on-march-21-2026-52-53f",
                    }
                ],
            }
        ],
        "markets": [],
        "unused": True,
    }
    assert validate_gamma_search_payload(payload).status == "ok"


@pytest.mark.parametrize(
    "payload",
    [
        {"timezone": "", "utc_offset_seconds": 0, "hourly": {"time": [], "temperature_2m": []}},
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": True,
            "hourly": {"time": [], "temperature_2m": []},
            "hourly_units": {"temperature_2m": "°C"},
        },
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": 0,
            "hourly": {"time": ["2026-07-15T00:00"], "temperature_2m": [1.0, 2.0]},
            "hourly_units": {"temperature_2m": "°C"},
        },
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": 0,
            "hourly": {"time": ["bad"], "temperature_2m": [1.0]},
            "hourly_units": {"temperature_2m": "°C"},
        },
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": 0,
            "hourly": {"time": ["2026-07-15T00:00"], "temperature_2m": [True]},
            "hourly_units": {"temperature_2m": "°C"},
        },
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": 0,
            "hourly": {"time": ["2026-07-15T00:00"], "temperature_2m": [1.0]},
            "hourly_units": {"temperature_2m": "°F"},
        },
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": 0,
            "hourly": {
                "time": ["2026-07-15T00:00"],
                "temperature_2m": [1.0],
                "precipitation": [0.0, 1.0],
            },
            "hourly_units": {"temperature_2m": "°C"},
        },
    ],
)
def test_single_run_malformed_fail_closed(payload: object) -> None:
    result = validate_single_run_payload(payload)
    assert result.status == "malformed"
    with pytest.raises(ProviderSchemaError):
        result.raise_for_status()


def test_single_run_empty_aligned_and_fixture_ok() -> None:
    empty = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 7200,
        "hourly_units": {"temperature_2m": "°C"},
        "hourly": {"time": [], "temperature_2m": []},
    }
    assert validate_single_run_payload(empty).status == "empty"
    assert validate_single_run_payload(empty).semantic_class == "valid_empty"
    assert validate_single_run_payload(_load("phase3_single_run_lfpg.json")).status == "ok"


def test_archive_empty_aligned_is_valid_empty() -> None:
    empty = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 7200,
        "daily_units": {"temperature_2m_max": "°C"},
        "daily": {"time": [], "temperature_2m_max": []},
    }
    result = validate_archive_payload(empty)
    assert result.status == "empty"
    assert result.semantic_class == "valid_empty"


def test_gamma_structurally_valid_but_weather_ineligible() -> None:
    """Counterexample: parent+conditionId only is schema-ok, Phase3-ineligible, not schema error."""
    from weather_alpha.research.provider_schema import classify_payload_semantic_class

    payload = {
        "events": [{"id": "parent-1", "markets": [{"conditionId": "c-1"}]}],
        "markets": [],
    }
    result = validate_gamma_search_payload(payload)
    assert result.status == "ok"
    assert result.phase3_eligibility == "ineligible"
    assert classify_payload_semantic_class(result) == "schema_valid_phase3_ineligible"
    result.raise_for_status()  # must not raise schema error


def test_semantic_classes_cover_exact_contract() -> None:
    from weather_alpha.research.provider_schema import (
        SchemaValidationResult,
        classify_payload_semantic_class,
    )

    assert (
        classify_payload_semantic_class(
            SchemaValidationResult(status="ok", provider="x", phase3_eligibility="eligible")
        )
        == "schema_valid_eligible"
    )
    assert (
        classify_payload_semantic_class(
            SchemaValidationResult(status="ok", provider="x", phase3_eligibility="ineligible")
        )
        == "schema_valid_phase3_ineligible"
    )
    assert (
        classify_payload_semantic_class(
            SchemaValidationResult(status="malformed", provider="x", detail="bad")
        )
        == "schema_error"
    )
    assert (
        classify_payload_semantic_class(SchemaValidationResult(status="empty", provider="x"))
        == "valid_empty"
    )
    assert (
        classify_payload_semantic_class(SchemaValidationResult(status="http_failure", provider="x"))
        == "http_network_failure"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": 0,
            "daily": {"time": ["2026-07-15"]},
            "daily_units": {"temperature_2m_max": "°C"},
        },
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": True,
            "daily": {"time": ["2026-07-15"], "temperature_2m_max": [1.0]},
            "daily_units": {"temperature_2m_max": "°C"},
        },
        {
            "timezone": "",
            "utc_offset_seconds": 0,
            "daily": {"time": ["2026-07-15"], "temperature_2m_max": [1.0]},
            "daily_units": {"temperature_2m_max": "°C"},
        },
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": 0,
            "daily": {"time": ["2026-07-15"], "temperature_2m_max": [1.0, 2.0]},
            "daily_units": {"temperature_2m_max": "°C"},
        },
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": 0,
            "daily": {"time": ["not-a-date"], "temperature_2m_max": [1.0]},
            "daily_units": {"temperature_2m_max": "°C"},
        },
        {
            "timezone": "Europe/Paris",
            "utc_offset_seconds": 0,
            "daily": {"time": ["2026-07-15"], "temperature_2m_max": ["x"]},
            "daily_units": {"temperature_2m_max": "°C"},
        },
    ],
)
def test_archive_malformed_fail_closed(payload: object) -> None:
    result = validate_archive_payload(payload)
    assert result.status == "malformed"
    with pytest.raises(ProviderSchemaError):
        result.raise_for_status()


def test_archive_fixture_ok_ignores_unused_hourly() -> None:
    assert validate_archive_payload(_load("phase3_archive_lfpg.json")).status == "ok"
