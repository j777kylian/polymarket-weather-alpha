"""Phase 3.5 adversarial fixture tests: leakage, books, identity, type confusion."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes import RecordingGetTransport
from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.phase35.bands import (
    assert_price_bands_registered,
    build_stratification,
    descriptive_price_band,
)
from weather_alpha.phase35.book import (
    ForwardLiquidityState,
    hypothetical_ask_vwap,
    is_executable_two_sided_book,
    validate_order_book,
)
from weather_alpha.phase35.bootstrap import (
    EventGroupMetric,
    assess_acceptance_thresholds,
    robustness_report,
)
from weather_alpha.phase35.checkpoints import (
    ForecastCandidate,
    decision_timestamp,
    select_checkpoint_inputs,
)
from weather_alpha.phase35.config import PRE_REGISTERED_CHECKPOINT_HOURS, Phase35ForwardConfig
from weather_alpha.phase35.contracts import (
    FORWARD_TRACK,
    HISTORICAL_TRACK,
    HistoricalDescriptivePrice,
    HistoricalSourceMode,
    HistoricalUniverseComplete,
    Phase35TypeConfusionError,
    reject_historical_as_executable,
    stable_forward_raw_provenance_path,
    storage_root_for,
)
from weather_alpha.phase35.coverage import (
    CoverageEvidenceDay,
    DateWindow,
    MissingInterval,
    PointInTimeIntegrity,
    ProviderCoverageEvidence,
    ProviderCoverageStatus,
    audit_historical_coverage,
    build_real_source_coverage_audit,
)
from weather_alpha.phase35.forward_collector import (
    ForwardBookCollector,
    ForwardCollectResult,
    ForwardCollectTarget,
)
from weather_alpha.phase35.readiness import (
    ForwardValidationMode,
    HistoricalQualificationInputs,
    ReadinessDimensions,
    ReadinessDimensionValue,
    assess_historical_source_readiness,
    derive_phase35_collection_ready,
    executable_two_sided_book_validated_from_results,
    forward_collection_ready_from_validation,
    historical_source_ready_from_coverage_status,
    run_offline_readiness,
)
from weather_alpha.phase35.reports import (
    build_forward_collection_audit_report,
    build_historical_coverage_report,
)
from weather_alpha.research.prices import PricePoint


def _forecast(available: datetime, *, issued: datetime | None = None) -> ForecastCandidate:
    issued_at = issued or (available - timedelta(hours=6))
    return ForecastCandidate(
        issued_at=issued_at,
        available_at=available,
        run_param=issued_at.strftime("%Y-%m-%dT%H:%M"),
    )


def test_checkpoints_are_exactly_pre_registered() -> None:
    assert PRE_REGISTERED_CHECKPOINT_HOURS == (48, 24, 12, 6, 3, 1)
    with pytest.raises(ValueError, match="not a pre-registered"):
        decision_timestamp("2026-07-15", "UTC", 7)


def test_future_forecast_leakage_rejected() -> None:
    decision_event = "2026-07-15"
    # 24h before Europe/Paris midnight 2026-07-15 => 2026-07-13 22:00 UTC
    decision = decision_timestamp(decision_event, "Europe/Paris", 24)
    future = _forecast(decision + timedelta(hours=1))
    selected = select_checkpoint_inputs(
        event_date=decision_event,
        timezone_name="Europe/Paris",
        lead_hours=24,
        canonical_event_key=("event_id", "e1"),
        forecasts=(future,),
        prices=(PricePoint(observed_at=decision - timedelta(hours=1), price=0.2),),
    )
    assert selected.status == "rejected"
    assert "forecast_future_only" in selected.rejection_reasons


def test_wrong_forecast_run_prefers_latest_available_not_future() -> None:
    decision_event = "2026-07-15"
    decision = decision_timestamp(decision_event, "Europe/Paris", 24)
    older = _forecast(decision - timedelta(hours=30))
    newer = _forecast(decision - timedelta(hours=2))
    future = _forecast(decision + timedelta(hours=3))
    selected = select_checkpoint_inputs(
        event_date=decision_event,
        timezone_name="Europe/Paris",
        lead_hours=24,
        canonical_event_key=("event_id", "e1"),
        forecasts=(older, future, newer),
        prices=(PricePoint(observed_at=decision - timedelta(minutes=5), price=0.33),),
    )
    assert selected.status == "selected"
    assert selected.forecast is not None
    assert selected.forecast.available_at == newer.available_at
    assert selected.forecast_age_seconds is not None
    assert selected.forecast_age_seconds >= 0


def test_post_decision_price_rejected() -> None:
    decision_event = "2026-07-15"
    decision = decision_timestamp(decision_event, "Europe/Paris", 12)
    selected = select_checkpoint_inputs(
        event_date=decision_event,
        timezone_name="Europe/Paris",
        lead_hours=12,
        canonical_event_key=("event_id", "e1"),
        forecasts=(_forecast(decision - timedelta(hours=1)),),
        prices=(PricePoint(observed_at=decision + timedelta(seconds=1), price=0.55),),
    )
    assert selected.status == "rejected"
    assert "price_post_decision_only" in selected.rejection_reasons


def test_missing_checkpoint_rejected() -> None:
    with pytest.raises(ValueError, match="not a pre-registered"):
        select_checkpoint_inputs(
            event_date="2026-07-15",
            timezone_name="UTC",
            lead_hours=9,
            canonical_event_key=("event_id", "e1"),
            forecasts=(),
            prices=(),
        )


def test_malformed_and_crossed_and_insufficient_book() -> None:
    malformed = validate_order_book({"bids": "bad", "asks": []})
    assert malformed.status == "invalid"
    assert malformed.liquidity_state is ForwardLiquidityState.MALFORMED
    assert any("bids" in reason for reason in malformed.reasons)

    empty = validate_order_book({"bids": [], "asks": []})
    assert empty.status == "invalid"
    assert empty.liquidity_state is ForwardLiquidityState.EMPTY

    crossed = validate_order_book(
        {
            "bids": [{"price": "0.60", "size": "10"}],
            "asks": [{"price": "0.50", "size": "10"}],
        }
    )
    assert crossed.status == "invalid"
    assert crossed.liquidity_state is ForwardLiquidityState.CROSSED_INVALID
    assert "crossed_book" in crossed.reasons
    assert is_executable_two_sided_book(crossed) is False

    ok = validate_order_book(
        {
            "bids": [{"price": "0.40", "size": "5"}],
            "asks": [{"price": "0.42", "size": "5"}],
        }
    )
    assert ok.status == "ok"
    assert ok.liquidity_state is ForwardLiquidityState.TWO_SIDED
    assert is_executable_two_sided_book(ok) is True
    fill = hypothetical_ask_vwap(ok, size=20.0)
    assert fill.insufficient_depth is True
    assert fill.vwap_entry is None
    assert fill.fee_status == "unknown"
    assert fill.fee_rate is None


def test_one_sided_books_are_valid_observations_not_executable() -> None:
    ask_only = validate_order_book(
        {
            "market": "cond-a",
            "asset_id": "tok-a",
            "bids": [],
            "asks": [{"price": "0.42", "size": "32"}],
        }
    )
    assert ask_only.status == "ok"
    assert ask_only.liquidity_state is ForwardLiquidityState.ASK_ONLY
    assert ask_only.best_ask == 0.42
    assert ask_only.best_bid is None
    assert is_executable_two_sided_book(ask_only) is False
    ask_fill = hypothetical_ask_vwap(ask_only, size=10.0)
    assert ask_fill.vwap_entry is None
    assert ask_fill.insufficient_depth is True

    bid_only = validate_order_book(
        {
            "market": "cond-a",
            "asset_id": "tok-a",
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [],
        }
    )
    assert bid_only.status == "ok"
    assert bid_only.liquidity_state is ForwardLiquidityState.BID_ONLY
    assert bid_only.best_bid == 0.40
    assert bid_only.best_ask is None
    assert is_executable_two_sided_book(bid_only) is False


def _fixture_book_payload(*, ask_price: str = "0.41") -> dict[str, object]:
    return {
        "market": "cond-a",
        "asset_id": "tok-a",
        "timestamp": 1_720_000_000_000,
        "bids": [{"price": "0.4", "size": "10"}],
        "asks": [{"price": ask_price, "size": "10"}],
    }


def _collect_forward_snapshot(
    output_root: Path, payload: Mapping[str, object]
) -> ForwardCollectResult:
    transport = RecordingGetTransport({"https://clob.polymarket.com/book": payload})
    collector = ForwardBookCollector(
        ReadOnlyHttpClient(transport=transport, max_retries=0),
        output_root=output_root,
        retrieved_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )
    return collector.collect_one(
        ForwardCollectTarget(
            canonical_event_id="e1",
            condition_id="cond-a",
            market_id="m1",
            token_id="tok-a",
            city="paris",
            station_icao="LFPG",
            event_date="2026-07-15",
            native_unit="celsius",
            bucket_definition="30°C",
            decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            checkpoint_lead_hours=24,
        )
    )


def test_one_sided_forward_observation_retained_with_provenance(tmp_path: Path) -> None:
    payload = {
        "market": "cond-a",
        "asset_id": "tok-a",
        "timestamp": 1_720_000_000_000,
        "bids": [],
        "asks": [{"price": "0.55", "size": "32"}],
    }
    result = _collect_forward_snapshot(tmp_path / "forward", payload)
    assert result.quarantined is False
    assert result.snapshot is not None
    assert result.liquidity_state == ForwardLiquidityState.ASK_ONLY.value
    assert result.executable_two_sided is False
    assert result.snapshot.liquidity_state == ForwardLiquidityState.ASK_ONLY.value
    assert result.snapshot.asks
    assert not result.snapshot.bids
    dict_row = result.snapshot.as_dict()
    assert dict_row["raw_path"].startswith("forward/raw/")
    assert dict_row["content_sha256"]
    assert "\\" not in dict_row["raw_path"]
    for leak in ("/tmp/", "/Users/", "/home/"):
        assert leak not in json.dumps(dict_row, sort_keys=True)


def test_malformed_and_crossed_forward_fail_closed_with_raw_provenance(tmp_path: Path) -> None:
    crossed = {
        "market": "cond-a",
        "asset_id": "tok-a",
        "timestamp": 1_720_000_000_000,
        "bids": [{"price": "0.60", "size": "10"}],
        "asks": [{"price": "0.50", "size": "10"}],
    }
    crossed_result = _collect_forward_snapshot(
        tmp_path / "forward-crossed" / "data" / "phase35" / "forward", crossed
    )
    assert crossed_result.quarantined is True
    assert crossed_result.snapshot is None
    assert crossed_result.executable_two_sided is False
    assert crossed_result.liquidity_state == ForwardLiquidityState.CROSSED_INVALID.value
    assert crossed_result.raw_path is not None
    assert stable_forward_raw_provenance_path(crossed_result.raw_path).startswith("forward/raw/")

    empty = {
        "market": "cond-a",
        "asset_id": "tok-a",
        "timestamp": 1_720_000_000_000,
        "bids": [],
        "asks": [],
    }
    empty_result = _collect_forward_snapshot(
        tmp_path / "forward-empty" / "data" / "phase35" / "forward", empty
    )
    assert empty_result.quarantined is True
    assert empty_result.snapshot is None
    assert empty_result.liquidity_state == ForwardLiquidityState.EMPTY.value

    malformed = {
        "market": "cond-a",
        "asset_id": "tok-a",
        "bids": "nope",
        "asks": [{"price": "0.5", "size": "1"}],
    }
    bad = _collect_forward_snapshot(
        tmp_path / "forward-bad" / "data" / "phase35" / "forward", malformed
    )
    assert bad.quarantined is True
    assert bad.snapshot is None
    assert bad.liquidity_state == ForwardLiquidityState.MALFORMED.value


def test_event_identity_mismatch_quarantines(tmp_path: Path) -> None:
    payload = {
        "market": "other-condition",
        "asset_id": "tok-a",
        "timestamp": 1_720_000_000_000,
        "bids": [{"price": "0.4", "size": "10"}],
        "asks": [{"price": "0.41", "size": "10"}],
    }
    transport = RecordingGetTransport({"https://clob.polymarket.com/book": payload})
    collector = ForwardBookCollector(
        ReadOnlyHttpClient(transport=transport, max_retries=0),
        output_root=tmp_path / "forward",
        retrieved_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )
    result = collector.collect_one(
        ForwardCollectTarget(
            canonical_event_id="e1",
            condition_id="cond-a",
            market_id="m1",
            token_id="tok-a",
            city="paris",
            station_icao="LFPG",
            event_date="2026-07-15",
            native_unit="celsius",
            bucket_definition="30°C",
            decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            checkpoint_lead_hours=24,
        )
    )
    assert result.quarantined is True
    assert any("event_identity_mismatch" in reason for reason in result.reasons)
    assert all(call[0] == "GET" for call in transport.calls)


def test_settlement_mismatch_raises(tmp_path: Path) -> None:
    payload = {
        "market": "cond-a",
        "asset_id": "tok-a",
        "timestamp": 1_720_000_000_000,
        "bids": [{"price": "0.4", "size": "10"}],
        "asks": [{"price": "0.41", "size": "10"}],
    }
    transport = RecordingGetTransport({"https://clob.polymarket.com/book": payload})
    collector = ForwardBookCollector(
        ReadOnlyHttpClient(transport=transport, max_retries=0),
        output_root=tmp_path / "forward",
        retrieved_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )
    result = collector.collect_one(
        ForwardCollectTarget(
            canonical_event_id="e1",
            condition_id="cond-a",
            market_id="m1",
            token_id="tok-a",
            city="paris",
            station_icao="LFPG",
            event_date="2026-07-15",
            native_unit="celsius",
            bucket_definition="30°C",
            decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            checkpoint_lead_hours=24,
        )
    )
    assert result.snapshot is not None
    with pytest.raises(ValueError, match="settlement mismatch"):
        collector.append_settlement(
            result.snapshot,
            settlement_label="No",
            expected_label="Yes",
        )


def test_type_confusion_historical_not_executable() -> None:
    price = HistoricalDescriptivePrice(
        track=HISTORICAL_TRACK,
        observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
        descriptive_probability=0.07,
        token_id="t",
        condition_id="c",
        canonical_event_key=("event_id", "e"),
    )
    with pytest.raises(Phase35TypeConfusionError):
        price.as_executable_price()
    with pytest.raises(Phase35TypeConfusionError):
        reject_historical_as_executable(price)
    assert price.executable_entry_price is None
    assert price.best_ask is None
    assert price.track != FORWARD_TRACK
    assert storage_root_for(HISTORICAL_TRACK) != storage_root_for(FORWARD_TRACK)


def test_coverage_audit_blocks_stitched_and_reports_max_period() -> None:
    days = []
    for i in range(10):
        day = f"2025-06-{i + 1:02d}"
        days.append(
            CoverageEvidenceDay(
                day=day,
                identity=True,
                settlement=True,
                price=True,
                ecmwf_single_runs_init=True,
                ecmwf_single_runs_availability=True,
                station=True,
                event_date=True,
                unit=True,
                provenance=True,
                used_stitched_historical_forecast=(i == 5),
            )
        )
    audit = audit_historical_coverage(
        proposed_start="2025-06-01",
        proposed_end="2025-06-10",
        evidence=days,
        require_full_year_days=365,
    )
    assert audit.status in {"partial", "blocked_no_full_collection"}
    assert audit.full_collection_authorized is False
    assert audit.descriptive_only is True
    assert audit.historical_source_ready is False
    assert audit.maximum_defensible_span_days == 5
    assert any("stitched" in reason for reason in audit.blocked_reasons)
    assert audit.missing_intervals


def test_price_bands_and_no_alpha_without_thresholds() -> None:
    assert assert_price_bands_registered() == (
        "<1c",
        "1-3c",
        "3-5c",
        "5-10c",
        "10-20c",
        ">20c",
    )
    assert descriptive_price_band(0.005) == "<1c"
    assert descriptive_price_band(0.25) == ">20c"
    keys = build_stratification(
        city="paris",
        station="LFPG",
        event_date="2026-07-15",
        lead_hours=24,
        bucket_kind="above",
        descriptive_probability=0.02,
        model_probability=0.15,
        raw_edge=0.13,
    )
    assert keys.center_tail == "tail"
    assert keys.price == "1-3c"
    groups = (
        EventGroupMetric(
            canonical_event_key=("event_id", "e1"),
            event_date="2026-07-01",
            city="paris",
            station="LFPG",
            lead_hours=24,
            metric=0.2,
        ),
    )
    acceptance = assess_acceptance_thresholds(groups, interpreted_leads=(24,))
    assert acceptance.alpha_claimed is False
    assert acceptance.status == "insufficient_sample"
    robustness = robustness_report(groups)
    assert robustness["alpha_conclusion"] == "no_alpha_conclusion"


def _qualification_ok(**overrides: object) -> HistoricalQualificationInputs:
    kwargs: dict[str, object] = {
        "meaningful_point_in_time_window": True,
        "point_in_time_integrity": True,
        "checkpoint_support": True,
        "clob_descriptive_coverage": True,
        "meaningful_rediscovery": True,
        "gamma_survivorship_limitation_explicit": True,
        "no_completeness_claim": True,
        "pre_registered_minimums_met": True,
        "historical_universe_complete": HistoricalUniverseComplete.NOT_PROVEN,
        "max_defensible_historical_window": "2025-06-01..2026-05-31",
        "gamma_rediscovery_rate": 0.8,
        "clob_descriptive_coverage_rate": 0.75,
    }
    kwargs.update(overrides)
    return HistoricalQualificationInputs(
        meaningful_point_in_time_window=bool(kwargs["meaningful_point_in_time_window"]),
        point_in_time_integrity=bool(kwargs["point_in_time_integrity"]),
        checkpoint_support=bool(kwargs["checkpoint_support"]),
        clob_descriptive_coverage=bool(kwargs["clob_descriptive_coverage"]),
        meaningful_rediscovery=bool(kwargs["meaningful_rediscovery"]),
        gamma_survivorship_limitation_explicit=bool(
            kwargs["gamma_survivorship_limitation_explicit"]
        ),
        no_completeness_claim=bool(kwargs["no_completeness_claim"]),
        pre_registered_minimums_met=bool(kwargs["pre_registered_minimums_met"]),
        historical_universe_complete=kwargs["historical_universe_complete"],  # type: ignore[arg-type]
        max_defensible_historical_window=kwargs["max_defensible_historical_window"],  # type: ignore[arg-type]
        gamma_rediscovery_rate=kwargs["gamma_rediscovery_rate"],  # type: ignore[arg-type]
        clob_descriptive_coverage_rate=kwargs["clob_descriptive_coverage_rate"],  # type: ignore[arg-type]
    )


def test_readiness_truth_table_seven_cases() -> None:
    ready = ReadinessDimensionValue.READY
    not_ready = ReadinessDimensionValue.NOT_READY
    unknown = ReadinessDimensionValue.UNKNOWN

    # 1) code+historical+forward ready, executable=no -> PHASE35_COLLECTION_READY=yes
    dims_1 = ReadinessDimensions(
        code_ready=ready,
        historical_source_ready=ready,
        forward_collection_ready=ready,
        forward_validation_mode=ForwardValidationMode.LIVE_SMOKE,
        historical_source_mode=HistoricalSourceMode.SURVIVORSHIP_LIMITED_DESCRIPTIVE,
        executable_two_sided_book_validated=False,
    )
    assert dims_1.phase35_collection_ready is True
    assert dims_1.as_dict()["EXECUTABLE_TWO_SIDED_BOOK_VALIDATED"] is False
    assert dims_1.as_dict()["PHASE35_COLLECTION_READY_NOT_EXECUTABLE_OR_PROFITABLE"] is True

    # 2) historical source insufficient -> overall=no
    assert (
        derive_phase35_collection_ready(
            code_ready=ready,
            historical_source_ready=not_ready,
            forward_collection_ready=ready,
        )
        is False
    )

    # 3) fixture-only forward, no live validation -> forward_collection_ready=no
    assert (
        forward_collection_ready_from_validation(
            validation_mode=ForwardValidationMode.FIXTURE_CONTRACT,
            live_observation_retained=True,
        )
        is not_ready
    )

    # 4) live real one-sided book collected -> forward may be yes; executable=no
    assert (
        forward_collection_ready_from_validation(
            validation_mode=ForwardValidationMode.LIVE_SMOKE,
            live_observation_retained=True,
        )
        is ready
    )
    assert (
        executable_two_sided_book_validated_from_results(
            validation_mode=ForwardValidationMode.LIVE_SMOKE,
            executable_flags=(False,),
        )
        is False
    )

    # 5) live real valid two-sided book -> forward=yes and executable=yes
    assert (
        executable_two_sided_book_validated_from_results(
            validation_mode=ForwardValidationMode.LIVE_SMOKE,
            executable_flags=(True,),
        )
        is True
    )
    dims_5 = ReadinessDimensions(
        code_ready=ready,
        historical_source_ready=ready,
        forward_collection_ready=ready,
        forward_validation_mode=ForwardValidationMode.LIVE_SMOKE,
        historical_source_mode=HistoricalSourceMode.SURVIVORSHIP_LIMITED_DESCRIPTIVE,
        executable_two_sided_book_validated=True,
    )
    assert dims_5.phase35_collection_ready is True
    assert dims_5.executable_two_sided_book_validated is True

    # 6) universe completeness=not_proven but survivorship_limited quals pass -> historical may be yes
    hist_ready, hist_mode, missing = assess_historical_source_readiness(_qualification_ok())
    assert missing == ()
    assert hist_ready is ready
    assert hist_mode is HistoricalSourceMode.SURVIVORSHIP_LIMITED_DESCRIPTIVE
    assert _qualification_ok().historical_universe_complete is HistoricalUniverseComplete.NOT_PROVEN

    # 7) survivorship limitation omitted/hidden -> historical_source_ready=no
    hist_no, mode_no, missing_no = assess_historical_source_readiness(
        _qualification_ok(gamma_survivorship_limitation_explicit=False)
    )
    assert hist_no is not_ready
    assert mode_no is HistoricalSourceMode.NOT_ESTABLISHED
    assert "gamma_survivorship_limitation_explicit" in missing_no

    # Unknown historical never grants overall ready; fixture coverage helpers unchanged.
    assert (
        derive_phase35_collection_ready(
            code_ready=ready,
            historical_source_ready=unknown,
            forward_collection_ready=ready,
        )
        is False
    )
    assert historical_source_ready_from_coverage_status("partial") is not_ready
    assert historical_source_ready_from_coverage_status("blocked_no_full_collection") is not_ready
    assert historical_source_ready_from_coverage_status("full_collection_supported") is ready
    assert historical_source_ready_from_coverage_status("mystery") is unknown


def test_no_hardcoded_real_world_final_readiness_state() -> None:
    """Unit tests must not bake in a final real-world PHASE35_COLLECTION_READY."""
    # Policy helpers are input-driven; offline fixture path remains not collection-ready.
    assert (
        derive_phase35_collection_ready(
            code_ready=ReadinessDimensionValue.READY,
            historical_source_ready=ReadinessDimensionValue.READY,
            forward_collection_ready=ReadinessDimensionValue.READY,
        )
        is True
    )
    assert (
        derive_phase35_collection_ready(
            code_ready=ReadinessDimensionValue.READY,
            historical_source_ready=ReadinessDimensionValue.NOT_READY,
            forward_collection_ready=ReadinessDimensionValue.READY,
        )
        is False
    )


def test_fixture_90_day_partial_sets_historical_false() -> None:
    start = datetime(2025, 6, 1, tzinfo=UTC).date()
    rows: list[CoverageEvidenceDay] = []
    for offset in range(365):
        day = (start + timedelta(days=offset)).isoformat()
        supported = offset < 90
        rows.append(
            CoverageEvidenceDay(
                day=day,
                identity=supported,
                settlement=supported,
                price=supported,
                ecmwf_single_runs_init=supported,
                ecmwf_single_runs_availability=supported,
                station=supported,
                event_date=supported,
                unit=supported,
                provenance=supported,
            )
        )
    audit = audit_historical_coverage(
        proposed_start="2025-06-01",
        proposed_end="2026-05-31",
        evidence=rows,
    )
    assert audit.status == "partial"
    assert audit.maximum_defensible_span_days == 90
    assert audit.historical_source_ready is False
    assert historical_source_ready_from_coverage_status(audit.status) is (
        ReadinessDimensionValue.NOT_READY
    )
    schema = audit.to_real_source_audit_schema(audit_mode="fixture_derived")
    assert schema.historical_source_ready is False
    assert schema.requested_historical_window.span_days == 365
    assert schema.maximum_defensible_contiguous_window.span_days == 90
    assert schema.known_gaps
    assert schema.survivorship_limitations


def test_coverage_reports_are_deterministic(tmp_path: Path) -> None:
    days = [
        CoverageEvidenceDay(
            day=f"2025-06-{i + 1:02d}",
            identity=True,
            settlement=True,
            price=True,
            ecmwf_single_runs_init=True,
            ecmwf_single_runs_availability=True,
            station=True,
            event_date=True,
            unit=True,
            provenance=True,
        )
        for i in range(3)
    ]
    audit = audit_historical_coverage(
        proposed_start="2025-06-01",
        proposed_end="2025-06-03",
        evidence=days,
        require_full_year_days=365,
    )
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    path_a = build_historical_coverage_report(audit, output_dir=out_a)
    path_b = build_historical_coverage_report(audit, output_dir=out_b)
    payload_a = json.loads(path_a.read_text(encoding="utf-8"))
    payload_b = json.loads(path_b.read_text(encoding="utf-8"))
    assert payload_a == payload_b
    assert payload_a["measured_data"]["historical_source_ready"] is False
    assert "real_source_coverage_audit" in payload_a["measured_data"]


def test_real_audit_serialization_preserves_windows_and_providers() -> None:
    result = build_real_source_coverage_audit(
        requested_historical_window=DateWindow(start="2025-01-01", end="2025-12-31", span_days=365),
        maximum_defensible_contiguous_window=DateWindow(
            start="2025-03-01", end="2025-08-31", span_days=184
        ),
        ecmwf_single_runs_coverage=ProviderCoverageEvidence(
            provider="open_meteo_ecmwf_ifs_single_runs",
            status=ProviderCoverageStatus.PARTIAL,
            evidence={"probed_cycles": ["00", "06", "12", "18"], "gaps_found": True},
            notes=("representative probe only",),
        ),
        gamma_discovery_coverage=ProviderCoverageEvidence(
            provider="polymarket_gamma_public_search",
            status=ProviderCoverageStatus.PARTIAL,
            evidence={"survivorship_risk": True},
        ),
        clob_price_history_coverage=ProviderCoverageEvidence(
            provider="polymarket_clob_prices_history",
            status=ProviderCoverageStatus.PARTIAL,
            evidence={"descriptive_only": True, "not_executable": True},
        ),
        known_gaps=(
            MissingInterval(
                start="2025-01-01",
                end="2025-02-28",
                reason="ecmwf_unavailable_in_probe",
            ),
        ),
        survivorship_limitations=(
            "Gamma public-search may omit delisted historical weather markets.",
        ),
        point_in_time_integrity=PointInTimeIntegrity(
            ecmwf_single_runs_only=True,
            no_stitched_historical_forecast_substitution=True,
            available_at_respected=True,
            integrity_status="preserved",
            notes=("schema serialization fixture; coverage result is data-driven",),
        ),
        coverage_status="partial",
        audit_mode="real_provider",
    )
    dumped = result.as_dict()
    assert dumped["requested_historical_window"]["start"] == "2025-01-01"
    assert dumped["requested_historical_window"]["span_days"] == 365
    assert dumped["maximum_defensible_contiguous_window"]["span_days"] == 184
    assert dumped["known_gaps"][0]["reason"] == "ecmwf_unavailable_in_probe"
    assert dumped["ecmwf_single_runs_coverage"]["status"] == "partial"
    assert dumped["gamma_discovery_coverage"]["status"] == "partial"
    assert dumped["clob_price_history_coverage"]["status"] == "partial"
    assert dumped["historical_source_ready"] is False
    assert "12-month pass" not in json.dumps(dumped).lower()


def test_offline_readiness_integration(tmp_path: Path) -> None:
    result = run_offline_readiness(output_dir=tmp_path)
    assert result.phase35_collection_ready is False
    assert result.code_ready is ReadinessDimensionValue.READY
    assert result.historical_source_ready is ReadinessDimensionValue.NOT_READY
    # Fixture-only must never establish forward live readiness.
    assert result.forward_collection_ready is ReadinessDimensionValue.NOT_READY
    assert result.dimensions.forward_validation_mode is ForwardValidationMode.FIXTURE_CONTRACT
    assert result.dimensions.historical_source_mode is HistoricalSourceMode.NOT_ESTABLISHED
    assert result.dimensions.historical_universe_complete is HistoricalUniverseComplete.NOT_PROVEN
    assert result.dimensions.executable_two_sided_book_validated is False
    assert result.coverage_status == "partial"
    assert result.maximum_defensible_span_days == 90
    assert "collection_ready" not in result.as_dict()
    assert result.as_dict()["PHASE35_COLLECTION_READY"] is False
    assert result.as_dict()["HISTORICAL_SOURCE_READY"] == "not_ready"
    assert result.as_dict()["FORWARD_COLLECTION_READY"] == "not_ready"
    assert result.as_dict()["EXECUTABLE_TWO_SIDED_BOOK_VALIDATED"] is False
    assert result.as_dict()["HISTORICAL_SOURCE_MODE"] == "not_established"
    assert result.as_dict()["HISTORICAL_UNIVERSE_COMPLETE"] == "not_proven"
    assert "CHECKPOINT_SUPPORT" in result.as_dict()
    assert result.as_dict()["PHASE35_COLLECTION_READY_NOT_EXECUTABLE_OR_PROFITABLE"] is True
    assert len(result.report_paths) == 6
    for path in result.report_paths:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        for section in (
            "measured_data",
            "model_output",
            "assumptions",
            "missing_data",
            "inferences",
            "limitations",
        ):
            assert section in payload
        # Candidate-run count and selected checkpoint-cell count do not define
        # a meaningful cache-reuse metric; canonical reports deliberately omit it.
        assert "cache_reuse_count" not in json.dumps(payload, sort_keys=True)
    coverage_payload = json.loads(Path(result.report_paths[0]).read_text(encoding="utf-8"))
    assert coverage_payload["measured_data"]["historical_source_ready"] is False
    readiness_payload = json.loads(Path(result.report_paths[-1]).read_text(encoding="utf-8"))
    measured = readiness_payload["measured_data"]
    for key in (
        "CODE_READY",
        "HISTORICAL_SOURCE_READY",
        "HISTORICAL_SOURCE_MODE",
        "MAX_DEFENSIBLE_HISTORICAL_WINDOW",
        "ECMWF_GRID_TOTAL",
        "ECMWF_GRID_SUCCESS",
        "ECMWF_GRID_FAILURES",
        "CHECKPOINT_SUPPORT",
        "GAMMA_SURVIVORSHIP_LIMITATION",
        "HISTORICAL_UNIVERSE_COMPLETE",
        "GAMMA_REDISCOVERY_RATE",
        "CLOB_DESCRIPTIVE_COVERAGE",
        "FORWARD_COLLECTION_READY",
        "FORWARD_VALIDATION_MODE",
        "FORWARD_OBSERVATIONS",
        "FORWARD_LIQUIDITY_STATE_COUNTS",
        "EXECUTABLE_TWO_SIDED_BOOK_VALIDATED",
        "PHASE35_COLLECTION_READY",
    ):
        assert key in measured
    notes_blob = " ".join(str(n) for n in readiness_payload["limitations"]["notes"]).lower()
    assert "does not imply" in notes_blob
    assert "executable" in notes_blob or "profitable" in notes_blob
    assert (tmp_path / "data" / "phase35" / "historical").is_dir()
    assert (tmp_path / "data" / "phase35" / "forward").is_dir()
    cfg = Phase35ForwardConfig()
    assert cfg.fee_rate is None
    assert cfg.fee_status == "unknown"


def test_stable_forward_raw_provenance_path_uses_posix_forward_raw() -> None:
    absolute = Path(
        "/var/folders/xy/out/data/phase35/forward/raw/polymarket_clob-book/ab/abcdef0123456789.json"
    )
    stable = stable_forward_raw_provenance_path(absolute)
    assert stable == "forward/raw/polymarket_clob-book/ab/abcdef0123456789.json"
    assert "\\" not in stable
    assert "/" in stable
    assert stable == stable_forward_raw_provenance_path(stable)
    with pytest.raises(ValueError, match="forward/raw/"):
        stable_forward_raw_provenance_path("/tmp/unrelated/file.json")


def test_forward_snapshot_canonical_raw_path_independent_of_output_root(
    tmp_path: Path,
) -> None:
    payload = _fixture_book_payload()
    root_a = tmp_path / "ready-a" / "forward"
    root_b = tmp_path / "ready-b" / "forward"
    result_a = _collect_forward_snapshot(root_a, payload)
    result_b = _collect_forward_snapshot(root_b, payload)
    assert result_a.snapshot is not None
    assert result_b.snapshot is not None
    # Runtime may retain absolute storage paths for local file access.
    assert Path(result_a.snapshot.raw_path).is_absolute()
    assert Path(result_b.snapshot.raw_path).is_absolute()
    assert result_a.snapshot.raw_path != result_b.snapshot.raw_path

    dict_a = result_a.snapshot.as_dict()
    dict_b = result_b.snapshot.as_dict()
    assert dict_a == dict_b
    assert dict_a["content_sha256"] == dict_b["content_sha256"]
    assert dict_a["content_sha256"]
    assert dict_a["raw_path"] == dict_b["raw_path"]
    assert dict_a["raw_path"].startswith("forward/raw/")
    assert "\\" not in dict_a["raw_path"]
    assert dict_a["raw_path"] == Path(dict_a["raw_path"]).as_posix()
    serialized = json.dumps(dict_a, sort_keys=True)
    for leak in ("/tmp/", "/Users/", "/home/", str(root_a), str(root_b)):
        assert leak not in serialized
        assert leak not in dict_a["raw_path"]

    # Root-only change must not alter canonical identity.
    assert dict_a["raw_path"] == stable_forward_raw_provenance_path(result_a.snapshot.raw_path)
    assert dict_b["raw_path"] == stable_forward_raw_provenance_path(result_b.snapshot.raw_path)

    # Content change must change SHA-256 and stable provenance path.
    changed = _collect_forward_snapshot(root_a, _fixture_book_payload(ask_price="0.55"))
    assert changed.snapshot is not None
    changed_dict = changed.snapshot.as_dict()
    assert changed_dict["content_sha256"] != dict_a["content_sha256"]
    assert changed_dict["raw_path"] != dict_a["raw_path"]
    assert changed_dict["raw_path"].startswith("forward/raw/")


def test_forward_collection_audit_byte_identical_across_distinct_roots(
    tmp_path: Path,
) -> None:
    payload = _fixture_book_payload()
    root_a = tmp_path / "p35-out-a"
    root_b = tmp_path / "p35-out-b"
    snap_a = _collect_forward_snapshot(root_a / "data" / "phase35" / "forward", payload).snapshot
    snap_b = _collect_forward_snapshot(root_b / "data" / "phase35" / "forward", payload).snapshot
    assert snap_a is not None and snap_b is not None
    assert snap_a.as_dict() == snap_b.as_dict()

    path_a = build_forward_collection_audit_report(
        snapshots=(snap_a,),
        quarantined=0,
        output_dir=root_a,
    )
    path_b = build_forward_collection_audit_report(
        snapshots=(snap_b,),
        quarantined=0,
        output_dir=root_b,
    )
    json_a = path_a.read_bytes()
    json_b = path_b.read_bytes()
    md_a = path_a.with_suffix(".md").read_bytes()
    md_b = path_b.with_suffix(".md").read_bytes()
    assert json_a == json_b
    assert md_a == md_b

    text = json_a.decode("utf-8")
    for leak in ("/tmp/", "/Users/", "/home/", str(root_a), str(root_b)):
        assert leak not in text
    body = json.loads(json_a)
    raw_path = body["measured_data"]["snapshots"][0]["raw_path"]
    sha = body["measured_data"]["snapshots"][0]["content_sha256"]
    assert raw_path.startswith("forward/raw/")
    assert "/" in raw_path and "\\" not in raw_path
    assert sha
    assert sha == snap_a.content_sha256 == snap_b.content_sha256


def test_offline_readiness_canonical_artifacts_byte_identical_across_roots(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "ready-root-a"
    root_b = tmp_path / "ready-root-b"
    result_a = run_offline_readiness(output_dir=root_a)
    result_b = run_offline_readiness(output_dir=root_b)
    assert result_a.as_dict()["PHASE35_COLLECTION_READY"] is False
    assert result_b.as_dict()["PHASE35_COLLECTION_READY"] is False

    names = (
        "phase35_historical_coverage",
        "phase35_historical_calibration",
        "phase35_historical_tail_analysis",
        "phase35_forward_collection_audit",
        "phase35_forward_executability",
        "phase35_readiness",
    )
    for name in names:
        json_a = (root_a / "reports" / f"{name}.json").read_bytes()
        json_b = (root_b / "reports" / f"{name}.json").read_bytes()
        md_a = (root_a / "reports" / f"{name}.md").read_bytes()
        md_b = (root_b / "reports" / f"{name}.md").read_bytes()
        assert json_a == json_b, name
        assert md_a == md_b, name
        decoded = json_a.decode("utf-8")
        for leak in ("/tmp/", "/Users/", "/home/", str(root_a), str(root_b)):
            assert leak not in decoded, f"{name} leaked {leak}"

    audit = json.loads((root_a / "reports" / "phase35_forward_collection_audit.json").read_text())
    snap = audit["measured_data"]["snapshots"][0]
    assert snap["raw_path"].startswith("forward/raw/")
    assert snap["content_sha256"]
    assert snap["raw_path"] == Path(snap["raw_path"]).as_posix()
