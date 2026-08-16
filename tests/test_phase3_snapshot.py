"""Phase 3 station catalog, ResearchSnapshot UTC, and unknown-station quarantine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from weather_alpha.config.stations import load_stations
from weather_alpha.research.stations import PHASE3_REQUIRED_STATION_IDS, resolve_research_station
from weather_alpha.research.types import ResearchSnapshot, snapshot_dedup_key


def test_phase3_catalog_includes_configured_airports_and_klga() -> None:
    stations = load_stations()
    ids = {station.station_id for station in stations}
    assert ids >= PHASE3_REQUIRED_STATION_IDS
    assert ids >= {"LFPG", "EGLC", "EDDM", "EHAM", "LIMC", "KLGA"}
    klga = next(s for s in stations if s.station_id == "KLGA")
    assert klga.city == "new york"
    assert klga.timezone_name == "America/New_York"


def test_lfpb_is_not_remapped_to_lfpg() -> None:
    resolved, reason = resolve_research_station("LFPB")
    assert resolved is None
    assert reason is not None
    assert "LFPB" in reason
    assert "LFPG" not in reason or "not remapped" in reason.lower() or "unknown" in reason.lower()


def test_unknown_station_is_quarantined() -> None:
    resolved, reason = resolve_research_station("XXXX")
    assert resolved is None
    assert reason is not None


def test_known_station_is_preserved() -> None:
    resolved, reason = resolve_research_station("KLGA")
    assert resolved is not None
    assert resolved.station_id == "KLGA"
    assert reason is None


def test_research_snapshot_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ResearchSnapshot(
            condition_id="c",
            market_id="m",
            token_id="t",
            city="paris",
            station_icao="LFPG",
            event_date="2026-07-15",
            bucket_label="31°C",
            bucket_kind="exact",
            temperature_celsius_min=31.0,
            temperature_celsius_max=31.0,
            decision_ts=datetime(2026, 7, 14, 12, 0, 0),
            market_probability=0.4,
            executable_entry_price=None,
            best_bid=None,
            best_ask=None,
            midpoint=None,
            spread=None,
            volume=None,
            liquidity=None,
            weather_issued_at=datetime(2026, 7, 14, 0, 0, tzinfo=UTC),
            weather_available_at=datetime(2026, 7, 14, 6, 0, tzinfo=UTC),
            forecast_daily_max_c=28.0,
            observation_max_so_far_c=None,
            observation_as_of=None,
            settlement_label="Yes",
            diagnostic_actual_max_c=31.2,
            provenance_urls=(),
            raw_paths=(),
            content_hashes=(),
            limitations=("test",),
        )


def test_snapshot_dedup_key_is_deterministic() -> None:
    ts = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    a = _minimal_snapshot(decision_ts=ts, token_id="yes-1")
    b = _minimal_snapshot(decision_ts=ts, token_id="yes-1")
    c = _minimal_snapshot(decision_ts=ts, token_id="yes-2")
    assert snapshot_dedup_key(a) == snapshot_dedup_key(b)
    assert snapshot_dedup_key(a) != snapshot_dedup_key(c)


def _minimal_snapshot(*, decision_ts: datetime, token_id: str) -> ResearchSnapshot:
    return ResearchSnapshot(
        condition_id="cond",
        market_id="mkt",
        token_id=token_id,
        city="paris",
        station_icao="LFPG",
        event_date="2026-07-15",
        bucket_label="31°C",
        bucket_kind="exact",
        temperature_celsius_min=31.0,
        temperature_celsius_max=31.0,
        decision_ts=decision_ts,
        market_probability=0.4,
        executable_entry_price=None,
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        volume=None,
        liquidity=None,
        weather_issued_at=datetime(2026, 7, 14, 0, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 14, 6, 0, tzinfo=UTC),
        forecast_daily_max_c=28.0,
        observation_max_so_far_c=None,
        observation_as_of=None,
        settlement_label="Yes",
        diagnostic_actual_max_c=31.2,
        provenance_urls=("https://example.test/a",),
        raw_paths=("/tmp/a.json",),
        content_hashes=("abc",),
        limitations=("price-history p is descriptive only",),
    )
