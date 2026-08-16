"""Blocker 2: authoritative canonical_event_key with provenance and fail-closed ambiguity."""

from __future__ import annotations

from datetime import UTC, datetime

from weather_alpha.collectors.polymarket.collector import _markets_from_search
from weather_alpha.research.types import (
    CanonicalEventIdentity,
    ResearchSnapshot,
    build_canonical_event_identity,
    event_group_key,
)


def _snap(
    *,
    token: str,
    event_date: str,
    bucket: str,
    event_id: str | None = None,
    city: str = "paris",
    station: str = "LFPG",
    condition: str | None = None,
    question: str | None = None,
    slug: str | None = None,
    event_slug: str | None = None,
    neg_risk_market_id: str | None = None,
) -> ResearchSnapshot:
    return ResearchSnapshot(
        condition_id=condition or f"{token}-cond",
        market_id="m",
        token_id=token,
        city=city,
        station_icao=station,
        event_date=event_date,
        bucket_label=bucket,
        bucket_kind="exact",
        temperature_celsius_min=31.0,
        temperature_celsius_max=31.0,
        decision_ts=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        market_probability=0.4,
        executable_entry_price=None,
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        volume=None,
        liquidity=None,
        weather_issued_at=None,
        weather_available_at=None,
        forecast_daily_max_c=30.0,
        observation_max_so_far_c=None,
        observation_as_of=None,
        settlement_label="No",
        diagnostic_actual_max_c=31.0,
        provenance_urls=(),
        raw_paths=(),
        content_hashes=(),
        limitations=("test",),
        event_id=event_id,
        question=question,
        slug=slug,
        event_slug=event_slug,
        neg_risk_market_id=neg_risk_market_id,
    )


def test_explicit_event_id_groups_siblings() -> None:
    a = _snap(token="a", event_date="2026-07-15", bucket="30°C", event_id="evt-1")
    b = _snap(token="b", event_date="2026-07-15", bucket="31°C", event_id="evt-1")
    assert event_group_key(a) == event_group_key(b)
    identity = build_canonical_event_identity(a)
    assert identity.canonical_event_key == ("event_id", "evt-1")
    assert identity.source == "event_id"
    assert "event_id" in identity.evidence_fields


def test_parent_condition_or_neg_risk_groups_when_event_id_missing() -> None:
    a = _snap(
        token="a",
        event_date="2026-07-15",
        bucket="30°C",
        event_id=None,
        neg_risk_market_id="neg-family-1",
        city="paris",
        station="LFPG",
    )
    b = _snap(
        token="b",
        event_date="2026-07-15",
        bucket="31°C",
        event_id=None,
        neg_risk_market_id="neg-family-1",
        city="paris",
        station="LFPG",
    )
    assert event_group_key(a) == event_group_key(b)
    assert build_canonical_event_identity(a).source == "neg_risk_market_id"


def test_slug_family_groups_compatible_outcome_siblings() -> None:
    a = _snap(
        token="a",
        event_date="2026-03-21",
        bucket="52-53°F",
        city="new york",
        station="KLGA",
        event_slug="highest-temperature-in-nyc-on-march-21-2026",
        slug="highest-temperature-in-nyc-on-march-21-2026-52-53f",
        question="Will the highest temperature in New York City be between 52-53°F on March 21?",
    )
    b = _snap(
        token="b",
        event_date="2026-03-21",
        bucket="54-55°F",
        city="new york",
        station="KLGA",
        event_slug="highest-temperature-in-nyc-on-march-21-2026",
        slug="highest-temperature-in-nyc-on-march-21-2026-54-55f",
        question="Will the highest temperature in New York City be between 54-55°F on March 21?",
    )
    assert event_group_key(a) == event_group_key(b)
    identity = build_canonical_event_identity(a)
    assert identity.source in {"event_slug", "slug_family"}
    assert identity.ambiguous is False


def test_same_city_station_date_different_families_must_not_merge() -> None:
    a = _snap(
        token="a",
        event_date="2026-03-21",
        bucket="52-53°F",
        city="new york",
        station="KLGA",
        event_slug="highest-temperature-in-nyc-on-march-21-2026",
        question="Will the highest temperature in New York City be between 52-53°F on March 21?",
    )
    b = _snap(
        token="b",
        event_date="2026-03-21",
        bucket="rain",
        city="new york",
        station="KLGA",
        event_slug="will-it-rain-in-nyc-on-march-21-2026",
        question="Will it rain in New York City on March 21?",
    )
    assert event_group_key(a) != event_group_key(b)


def test_city_station_date_alone_is_not_sole_fallback_identity() -> None:
    a = _snap(
        token="a",
        event_date="2026-07-15",
        bucket="30°C",
        event_id=None,
        city="paris",
        station="LFPG",
        question=None,
        slug=None,
        event_slug=None,
    )
    identity = build_canonical_event_identity(a)
    assert identity.ambiguous is True
    assert identity.canonical_event_key[0] == "ambiguous"
    assert "city_station_date" not in identity.canonical_event_key[0]
    # Keys must remain unique per market rather than merging on city/station/date alone.
    b = _snap(
        token="b",
        event_date="2026-07-15",
        bucket="31°C",
        event_id=None,
        city="paris",
        station="LFPG",
        question=None,
        slug=None,
        event_slug=None,
        condition="other-cond",
    )
    assert event_group_key(a) != event_group_key(b)


def test_ambiguous_identity_is_quarantined_not_merged() -> None:
    identity = build_canonical_event_identity(
        _snap(
            token="a",
            event_date="2026-07-15",
            bucket="30°C",
            event_id=None,
            question=None,
            slug=None,
        )
    )
    assert isinstance(identity, CanonicalEventIdentity)
    assert identity.ambiguous is True
    assert identity.quarantine_reason == "event_identity_ambiguous"


def test_canonical_key_is_deterministic_across_runs() -> None:
    snap = _snap(
        token="a",
        event_date="2026-03-21",
        bucket="52-53°F",
        city="new york",
        station="KLGA",
        event_id="279170",
        event_slug="highest-temperature-in-nyc-on-march-21-2026",
    )
    first = build_canonical_event_identity(snap)
    second = build_canonical_event_identity(snap)
    assert first.canonical_event_key == second.canonical_event_key
    assert first.as_dict() == second.as_dict()


def test_markets_from_search_propagate_parent_event_identity() -> None:
    payload = {
        "events": [
            {
                "id": "279170",
                "slug": "highest-temperature-in-nyc-on-march-21-2026",
                "negRiskMarketID": "neg-nyc-321",
                "markets": [
                    {
                        "conditionId": "0xabc",
                        "question": (
                            "Will the highest temperature in New York City be "
                            "between 52-53°F on March 21?"
                        ),
                        "slug": "highest-temperature-in-nyc-on-march-21-2026-52-53f",
                        "groupItemTitle": "52-53°F",
                    },
                    {
                        "conditionId": "0xdef",
                        "question": (
                            "Will the highest temperature in New York City be "
                            "between 54-55°F on March 21?"
                        ),
                        "slug": "highest-temperature-in-nyc-on-march-21-2026-54-55f",
                        "groupItemTitle": "54-55°F",
                    },
                ],
            }
        ],
        "markets": [],
    }
    markets = _markets_from_search(payload)
    assert len(markets) == 2
    assert markets[0]["eventId"] == "279170"
    assert markets[0]["event_slug"] == "highest-temperature-in-nyc-on-march-21-2026"
    assert markets[1]["eventId"] == "279170"
    assert markets[0].get("negRiskMarketID") == "neg-nyc-321"
