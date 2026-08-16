"""Blocker 2: event-group validation, conflict codes, provenance retention."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from weather_alpha.research.event_groups import (
    DUPLICATE_OUTCOME_CONFLICT,
    DUPLICATE_SNAPSHOT,
    EVENT_BUCKET_STRUCTURE_CONFLICT,
    EVENT_CITY_CONFLICT,
    EVENT_DATE_CONFLICT,
    EVENT_ID_CONFLICT,
    EVENT_MARKET_FAMILY_CONFLICT,
    EVENT_QUESTION_FAMILY_CONFLICT,
    EVENT_RESOLUTION_SOURCE_CONFLICT,
    EVENT_SETTLEMENT_CARDINALITY_CONFLICT,
    EVENT_SETTLEMENT_UNRESOLVED,
    EVENT_SLUG_FAMILY_CONFLICT,
    EVENT_STATION_CONFLICT,
    EVENT_UNIT_CONFLICT,
    accept_event_groups,
    normalize_station_icao,
    validate_candidate_event_group,
)
from weather_alpha.research.run import load_snapshots_from_jsonl
from weather_alpha.research.types import (
    EVENT_IDENTITY_AMBIGUOUS,
    ResearchSnapshot,
    build_canonical_event_identity,
    event_group_key,
)

_UNSET = object()


def _slug_bucket(bucket: str) -> str:
    text = bucket.lower().replace("°", "").replace(" ", "-")
    return text


def _bucket_native(
    bucket: str, unit: str | None
) -> tuple[str, float | None, float | None, float | None, float | None]:
    """Test helper: parse native bounds from fixture labels (not production inference)."""
    text = bucket.strip()
    lower = text.lower()
    below = re.search(r"([+-]?\d+(?:\.\d+)?)\s*°?\s*[cf]?\s*or\s+below", lower)
    if below:
        hi = float(below.group(1))
        return "below", None, hi, None, hi
    above = re.search(
        r"([+-]?\d+(?:\.\d+)?)\s*°?\s*[cf]?\s*or\s+(?:higher|above)",
        lower,
    )
    if above:
        lo = float(above.group(1))
        return "above", lo, None, lo, None
    ranged = re.search(
        r"([+-]?\d+(?:\.\d+)?)\s*[\-\u2013]\s*([+-]?\d+(?:\.\d+)?)",
        text,
    )
    if ranged:
        lo = float(ranged.group(1))
        hi = float(ranged.group(2))
        return "range", lo, hi, lo, hi
    exact = re.search(r"([+-]?\d+(?:\.\d+)?)", text)
    if exact:
        value = float(exact.group(1))
        return "exact", value, value, value, value
    return "exact", 31.0, 31.0, 31.0, 31.0


def _snap(
    *,
    token: str,
    event_date: str = "2026-07-15",
    bucket: str = "31°C",
    event_id: str | None = "evt-1",
    city: str | None = "paris",
    station: str | None = "LFPG",
    unit: str | None = "C",
    condition: str | None = None,
    market_id: str | None = None,
    question: str | object | None = _UNSET,
    slug: str | object | None = _UNSET,
    event_slug: str | None = "highest-temperature-in-paris-on-july-15-2026",
    neg_risk_market_id: str | None = "neg-1",
    settlement: str | None = "No",
    source_station: str | None = "LFPG",
    provenance_urls: tuple[str, ...] = (
        "https://www.wunderground.com/history/daily/fr/paris/LFPG",
    ),
    raw_paths: tuple[str, ...] = ("raw/a.json",),
    content_hashes: tuple[str, ...] = ("abc",),
    decision_ts: datetime | None = None,
    bucket_kind: str | object | None = _UNSET,
    temperature_native_min: float | object | None = _UNSET,
    temperature_native_max: float | object | None = _UNSET,
) -> ResearchSnapshot:
    # Explicit None must stay None (ambiguous-identity cases); omit params for defaults.
    resolved_question: str | None
    if question is _UNSET:
        resolved_question = f"Will the highest temperature in Paris be {bucket} on July 15?"
    else:
        resolved_question = question  # type: ignore[assignment]
    resolved_slug: str | None
    if slug is _UNSET:
        resolved_slug = f"highest-temperature-in-paris-on-july-15-2026-{_slug_bucket(bucket)}"
    else:
        resolved_slug = slug  # type: ignore[assignment]
    kind: str
    lo_c: float | None
    hi_c: float | None
    lo_n: float | None
    hi_n: float | None
    kind, lo_c, hi_c, lo_n, hi_n = _bucket_native(bucket, unit)
    if bucket_kind is not _UNSET:
        kind = bucket_kind  # type: ignore[assignment]
    if temperature_native_min is not _UNSET:
        lo_n = temperature_native_min  # type: ignore[assignment]
    if temperature_native_max is not _UNSET:
        hi_n = temperature_native_max  # type: ignore[assignment]
    return ResearchSnapshot(
        condition_id=condition or f"{token}-cond",
        market_id=market_id or f"m-{token}",
        token_id=token,
        city=city,
        station_icao=station,
        event_date=event_date,
        bucket_label=bucket,
        bucket_kind=kind,
        temperature_celsius_min=lo_c,
        temperature_celsius_max=hi_c,
        decision_ts=decision_ts or datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
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
        settlement_label=settlement,
        diagnostic_actual_max_c=31.0,
        provenance_urls=provenance_urls,
        raw_paths=raw_paths,
        content_hashes=content_hashes,
        limitations=("test",),
        event_id=event_id,
        question=resolved_question,
        slug=resolved_slug,
        event_slug=event_slug,
        neg_risk_market_id=neg_risk_market_id,
        temperature_unit=unit,
        temperature_native_min=lo_n,
        temperature_native_max=hi_n,
        source_station_icao=source_station,
    )


def test_coherent_siblings_accepted_with_provenance() -> None:
    a = _snap(token="a", bucket="30°C", settlement="Yes")
    b = _snap(token="b", bucket="31°C", settlement="No")
    result = validate_candidate_event_group((a, b))
    assert result.accepted is True
    assert result.quarantine_code is None
    accepted, quarantined = accept_event_groups((a, b))
    assert len(accepted) == 2
    assert quarantined == ()
    assert accepted[0].provenance_urls == a.provenance_urls
    assert accepted[1].raw_paths == b.raw_paths
    assert accepted[0].content_hashes == a.content_hashes


def test_city_station_date_alone_never_groups() -> None:
    a = _snap(
        token="a",
        event_id=None,
        event_slug=None,
        neg_risk_market_id=None,
        question=None,
        slug=None,
    )
    b = _snap(
        token="b",
        event_id=None,
        event_slug=None,
        neg_risk_market_id=None,
        question=None,
        slug=None,
        condition="other",
    )
    identity = build_canonical_event_identity(a)
    assert identity.ambiguous is True
    assert identity.quarantine_reason == EVENT_IDENTITY_AMBIGUOUS
    accepted, quarantined = accept_event_groups((a, b))
    assert accepted == ()
    assert {row.reason for row in quarantined} == {EVENT_IDENTITY_AMBIGUOUS}


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda s: _snap(token=s.token_id, event_id="other-evt"), EVENT_ID_CONFLICT),
        (lambda s: _snap(token=s.token_id, city="london"), EVENT_CITY_CONFLICT),
        (lambda s: _snap(token=s.token_id, station="EGLC"), EVENT_STATION_CONFLICT),
        (lambda s: _snap(token=s.token_id, event_date="2026-07-16"), EVENT_DATE_CONFLICT),
        (lambda s: _snap(token=s.token_id, unit="F"), EVENT_UNIT_CONFLICT),
        (
            lambda s: _snap(
                token=s.token_id,
                source_station="LFPB",
                provenance_urls=("https://www.wunderground.com/history/daily/fr/paris/LFPB",),
            ),
            EVENT_RESOLUTION_SOURCE_CONFLICT,
        ),
        (
            lambda s: _snap(
                token=s.token_id,
                event_slug="highest-temperature-in-paris-on-july-16-2026",
            ),
            EVENT_SLUG_FAMILY_CONFLICT,
        ),
        (
            lambda s: _snap(
                token=s.token_id,
                question="Will the highest temperature in London be 31°C on July 15?",
            ),
            EVENT_QUESTION_FAMILY_CONFLICT,
        ),
        (
            lambda s: _snap(token=s.token_id, neg_risk_market_id="neg-other"),
            EVENT_MARKET_FAMILY_CONFLICT,
        ),
    ],
)
def test_whole_group_conflicts_quarantine_all(
    mutator: Callable[[ResearchSnapshot], ResearchSnapshot], code: str
) -> None:
    a = _snap(token="a", bucket="30°C", settlement="Yes")
    b = mutator(_snap(token="b", bucket="31°C", settlement="No"))
    # Force same canonical key so they are candidate-grouped, then fail validation.
    object.__setattr__(b, "canonical_event_key", ("event_id", "evt-1"))
    object.__setattr__(a, "canonical_event_key", ("event_id", "evt-1"))
    result = validate_candidate_event_group((a, b))
    assert result.accepted is False
    assert result.quarantine_code == code
    accepted, quarantined = accept_event_groups((a, b))
    assert accepted == ()
    assert len(quarantined) == 2
    assert all(row.reason == code for row in quarantined)
    # Provenance retained on quarantine rows via details / member fields.
    assert {row.token_id for row in quarantined} == {"a", "b"}


def test_varying_market_ids_and_buckets_allowed() -> None:
    a = _snap(
        token="a",
        bucket="30°C",
        settlement="Yes",
        condition="cond-a",
        market_id="m-a",
        slug="highest-temperature-in-paris-on-july-15-2026-30c",
        question="Will the highest temperature in Paris be 30°C on July 15?",
    )
    b = _snap(
        token="b",
        bucket="31°C",
        settlement="No",
        condition="cond-b",
        market_id="m-b",
        slug="highest-temperature-in-paris-on-july-15-2026-31c",
        question="Will the highest temperature in Paris be 31°C on July 15?",
    )
    assert validate_candidate_event_group((a, b)).accepted is True


def test_duplicate_snapshot_isolated_when_remainder_coherent() -> None:
    a = _snap(token="a", bucket="30°C", settlement="Yes")
    dup = _snap(token="a", bucket="30°C", settlement="Yes")  # same dedup key
    b = _snap(token="b", bucket="31°C", settlement="No")
    accepted, quarantined = accept_event_groups((a, dup, b))
    assert {s.token_id for s in accepted} == {"a", "b"}
    assert len(quarantined) == 1
    assert quarantined[0].reason == DUPLICATE_SNAPSHOT
    assert quarantined[0].token_id == "a"


def test_duplicate_outcome_conflict_quarantines_group() -> None:
    a = _snap(token="a", bucket="31°C", settlement="Yes", condition="c1")
    b = _snap(token="b", bucket="31°C", settlement="No", condition="c2")
    result = validate_candidate_event_group((a, b))
    assert result.accepted is False
    assert result.quarantine_code == DUPLICATE_OUTCOME_CONFLICT


def test_bucket_structure_conflict() -> None:
    a = _snap(token="a", bucket="30°C", settlement="Yes")
    b = _snap(
        token="b",
        bucket="31°C",
        settlement="No",
        slug="highest-temperature-in-paris-on-july-15-2026-31c",
        question="Will the highest temperature in Paris be 31°C on July 15?",
    )
    object.__setattr__(b, "bucket_kind", "unknown")
    object.__setattr__(b, "temperature_celsius_min", None)
    object.__setattr__(b, "temperature_celsius_max", None)
    object.__setattr__(b, "temperature_native_min", None)
    object.__setattr__(b, "temperature_native_max", None)
    result = validate_candidate_event_group((a, b))
    assert result.accepted is False
    assert result.quarantine_code == EVENT_BUCKET_STRUCTURE_CONFLICT


def test_settlement_cardinality_and_unresolved() -> None:
    multi = (
        _snap(token="a", bucket="30°C", settlement="Yes"),
        _snap(token="b", bucket="31°C", settlement="Yes"),
    )
    assert (
        validate_candidate_event_group(multi).quarantine_code
        == EVENT_SETTLEMENT_CARDINALITY_CONFLICT
    )
    unresolved = (
        _snap(token="a", bucket="30°C", settlement="No"),
        _snap(token="b", bucket="31°C", settlement="No"),
    )
    assert validate_candidate_event_group(unresolved).quarantine_code == EVENT_SETTLEMENT_UNRESOLVED


def test_real_scale_fixture_shape_two_buckets_accepted() -> None:
    """Proxy for 1738/158: coherent event_id family remains accepted."""
    rows = [
        _snap(token=f"t{i}", bucket=f"{30 + i}°C", settlement="Yes" if i == 0 else "No")
        for i in range(11)
    ]
    accepted, quarantined = accept_event_groups(rows)
    assert len(accepted) == 11
    assert quarantined == ()


def test_normalize_station_icao_trim_upper_four_letters() -> None:
    assert normalize_station_icao(" lfpg ") == "LFPG"
    assert normalize_station_icao("Klga") == "KLGA"
    assert normalize_station_icao("LFP") is None
    assert normalize_station_icao("LFPGX") is None
    assert normalize_station_icao(None) is None
    assert normalize_station_icao("") is None


def test_station_case_variants_do_not_conflict() -> None:
    a = _snap(token="a", bucket="30°C", settlement="Yes", station="lfpg", source_station="lfpg")
    b = _snap(token="b", bucket="31°C", settlement="No", station="LFPG", source_station="LFPG")
    assert validate_candidate_event_group((a, b)).accepted is True


def test_weak_child_slug_family_does_not_veto_coherent_parent() -> None:
    """Imperfect child slug families are WEAK when parent event_id group is coherent."""
    a = _snap(
        token="a",
        bucket="30°C",
        settlement="Yes",
        slug="highest-temperature-in-paris-on-july-15-2026-30c",
    )
    b = _snap(
        token="b",
        bucket="31°C",
        settlement="No",
        # Imperfect child slug (missing '-on-') — different family, same city/date.
        slug="highest-temperature-in-paris-july-15-2026-31c",
    )
    result = validate_candidate_event_group((a, b))
    assert result.accepted is True
    accepted, quarantined = accept_event_groups((a, b))
    assert len(accepted) == 2
    assert quarantined == ()


def test_weak_imperfect_question_family_does_not_veto_coherent_parent() -> None:
    a = _snap(
        token="a",
        bucket="30°C",
        settlement="Yes",
        question="Will the highest temperature in Paris be 30°C on July 15?",
    )
    b = _snap(
        token="b",
        bucket="31°C",
        settlement="No",
        question="Highest temperature in Paris on July 15, 2026?",
    )
    result = validate_candidate_event_group((a, b))
    assert result.accepted is True


def test_positive_proof_other_city_in_question_still_quarantines() -> None:
    a = _snap(token="a", bucket="30°C", settlement="Yes")
    b = _snap(
        token="b",
        bucket="31°C",
        settlement="No",
        question="Will the highest temperature in London be 31°C on July 15?",
    )
    object.__setattr__(a, "canonical_event_key", ("event_id", "evt-1"))
    object.__setattr__(b, "canonical_event_key", ("event_id", "evt-1"))
    result = validate_candidate_event_group((a, b))
    assert result.accepted is False
    assert result.quarantine_code == EVENT_QUESTION_FAMILY_CONFLICT


def test_positive_proof_non_temperature_child_still_quarantines() -> None:
    a = _snap(token="a", bucket="30°C", settlement="Yes")
    b = _snap(
        token="b",
        bucket="rain",
        settlement="No",
        question="Will it rain in Paris on July 15?",
        slug="will-it-rain-in-paris-on-july-15-2026",
    )
    object.__setattr__(a, "canonical_event_key", ("event_id", "evt-1"))
    object.__setattr__(b, "canonical_event_key", ("event_id", "evt-1"))
    result = validate_candidate_event_group((a, b))
    assert result.accepted is False
    assert result.quarantine_code in {
        EVENT_QUESTION_FAMILY_CONFLICT,
        EVENT_SLUG_FAMILY_CONFLICT,
        EVENT_BUCKET_STRUCTURE_CONFLICT,
    }


def test_current_input_retains_1738_snapshots_and_158_groups() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "phase3"
        / "2026-03-20_2026-04-18_1h"
        / "phase3_snapshots.jsonl"
    )
    if not path.is_file():
        pytest.skip(f"current Phase 3 input missing: {path}")
    snapshots = load_snapshots_from_jsonl(path)
    assert len(snapshots) == 1738
    accepted, quarantined = accept_event_groups(snapshots)
    assert len(accepted) == 1738
    assert quarantined == ()
    groups = {event_group_key(row) for row in accepted}
    assert len(groups) == 158
    # Each group should be an 11-outcome family under current input.
    from collections import Counter

    sizes = Counter(event_group_key(row) for row in accepted)
    assert set(sizes.values()) == {11}


def test_lone_no_settlement_member_is_unresolved() -> None:
    """Integration root cause: single No-settled member fails yes_count==0 rule."""
    lone = _snap(token="ny", bucket="86°F", unit="F", settlement="No")
    result = validate_candidate_event_group((lone,))
    assert result.accepted is False
    assert result.quarantine_code == EVENT_SETTLEMENT_UNRESOLVED


def test_adjacent_pair_with_one_yes_accepts() -> None:
    family = (
        _snap(token="a", bucket="86°F", unit="F", settlement="No"),
        _snap(token="b", bucket="87°F", unit="F", settlement="Yes"),
    )
    assert validate_candidate_event_group(family).accepted is True


def test_overlap_range_buckets_quarantine_group() -> None:
    a = _snap(token="a", bucket="52-55°F", unit="F", settlement="Yes")
    b = _snap(token="b", bucket="54-57°F", unit="F", settlement="No")
    result = validate_candidate_event_group((a, b))
    assert result.accepted is False
    assert result.quarantine_code == EVENT_BUCKET_STRUCTURE_CONFLICT
    assert result.detail is not None
    assert "overlap" in result.detail.lower()
    accepted, quarantined = accept_event_groups((a, b))
    assert accepted == ()
    assert len(quarantined) == 2
    assert all(row.reason == EVENT_BUCKET_STRUCTURE_CONFLICT for row in quarantined)
    assert all(row.details and "urls=" in row.details for row in quarantined)


def test_exact_and_range_gaps_quarantine_group() -> None:
    exacts = (
        _snap(token="a", bucket="30°C", settlement="Yes"),
        _snap(token="b", bucket="32°C", settlement="No"),  # gap at 31
    )
    result = validate_candidate_event_group(exacts)
    assert result.accepted is False
    assert result.quarantine_code == EVENT_BUCKET_STRUCTURE_CONFLICT
    assert result.detail is not None
    assert "gap" in result.detail.lower()

    ranges = (
        _snap(token="c", bucket="52-53°F", unit="F", settlement="Yes"),
        _snap(token="d", bucket="56-57°F", unit="F", settlement="No"),  # gap 54-55
    )
    result_r = validate_candidate_event_group(ranges)
    assert result_r.accepted is False
    assert result_r.quarantine_code == EVENT_BUCKET_STRUCTURE_CONFLICT
    assert result_r.detail is not None
    assert "gap" in result_r.detail.lower()


def test_valid_adjacent_exact_and_range_under_settlement_semantics() -> None:
    celsius = (
        _snap(token="a", bucket="30°C", settlement="Yes"),
        _snap(token="b", bucket="31°C", settlement="No"),
    )
    assert validate_candidate_event_group(celsius).accepted is True

    fahrenheit = (
        _snap(token="c", bucket="52-53°F", unit="F", settlement="Yes"),
        _snap(token="d", bucket="54-55°F", unit="F", settlement="No"),
    )
    assert validate_candidate_event_group(fahrenheit).accepted is True


def test_valid_low_interior_high_tail_family() -> None:
    family = (
        _snap(token="lo", bucket="41°F or below", unit="F", settlement="No"),
        _snap(token="a", bucket="42-43°F", unit="F", settlement="Yes"),
        _snap(token="b", bucket="44-45°F", unit="F", settlement="No"),
        _snap(token="hi", bucket="46°F or higher", unit="F", settlement="No"),
    )
    result = validate_candidate_event_group(family)
    assert result.accepted is True
    assert result.quarantine_code is None


def test_invalid_tail_connection_quarantines() -> None:
    # below 41 then 43-44 skips 42 — invalid low-tail connection
    bad_low = (
        _snap(token="lo", bucket="41°F or below", unit="F", settlement="No"),
        _snap(token="a", bucket="43-44°F", unit="F", settlement="Yes"),
        _snap(token="hi", bucket="45°F or higher", unit="F", settlement="No"),
    )
    result = validate_candidate_event_group(bad_low)
    assert result.accepted is False
    assert result.quarantine_code == EVENT_BUCKET_STRUCTURE_CONFLICT
    assert result.detail is not None
    assert "tail" in result.detail.lower() or "gap" in result.detail.lower()


def test_duplicate_semantic_coverage_quarantines() -> None:
    # Distinct labels but identical native coverage
    a = _snap(token="a", bucket="31°C", settlement="Yes")
    b = _snap(
        token="b",
        bucket="31.0°C",
        settlement="No",
        temperature_native_min=31.0,
        temperature_native_max=31.0,
        bucket_kind="exact",
    )
    result = validate_candidate_event_group((a, b))
    assert result.accepted is False
    assert result.quarantine_code in {EVENT_BUCKET_STRUCTURE_CONFLICT, DUPLICATE_OUTCOME_CONFLICT}
    if result.quarantine_code == EVENT_BUCKET_STRUCTURE_CONFLICT:
        assert result.detail is not None
        assert "duplicate" in result.detail.lower() or "overlap" in result.detail.lower()


def test_numeric_native_fields_not_inferred_from_display_label() -> None:
    # Label says 30°C but native fields claim 40 — topology uses native only.
    a = _snap(
        token="a",
        bucket="30°C",
        settlement="Yes",
        temperature_native_min=40.0,
        temperature_native_max=40.0,
        bucket_kind="exact",
    )
    b = _snap(
        token="b",
        bucket="31°C",
        settlement="No",
        temperature_native_min=41.0,
        temperature_native_max=41.0,
        bucket_kind="exact",
    )
    # Adjacent natives 40/41 are valid even though labels say 30/31.
    assert validate_candidate_event_group((a, b)).accepted is True
    # Gap in native space must quarantine despite adjacent-looking labels.
    c = _snap(
        token="c",
        bucket="32°C",
        settlement="No",
        temperature_native_min=43.0,
        temperature_native_max=43.0,
        bucket_kind="exact",
    )
    result = validate_candidate_event_group((a, b, c))
    assert result.accepted is False
    assert result.quarantine_code == EVENT_BUCKET_STRUCTURE_CONFLICT
