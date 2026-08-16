"""Offline Phase 3.5 collection-readiness orchestration (fixtures only by default).

Readiness dimensions are semantically distinct:
- HISTORICAL_SOURCE_READY may use survivorship_limited_descriptive without universe completeness.
- FORWARD_COLLECTION_READY is observational (live) collection capability; fixture-only is never live-ready.
- EXECUTABLE_TWO_SIDED_BOOK_VALIDATED is separate and excluded from PHASE35_COLLECTION_READY.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyResponse
from weather_alpha.phase35.bands import assert_price_bands_registered, build_stratification
from weather_alpha.phase35.book import (
    ForwardLiquidityState,
    hypothetical_fills_for_config,
    is_executable_two_sided_book,
    validate_order_book,
)
from weather_alpha.phase35.bootstrap import (
    EventGroupMetric,
    assess_acceptance_thresholds,
    blocked_bootstrap_mean_ci,
    robustness_report,
)
from weather_alpha.phase35.checkpoints import (
    ForecastCandidate,
    registered_checkpoints,
    select_all_registered_checkpoints,
)
from weather_alpha.phase35.config import PRE_REGISTERED_CHECKPOINT_HOURS, Phase35ForwardConfig
from weather_alpha.phase35.contracts import (
    FORWARD_TRACK,
    HISTORICAL_TRACK,
    HistoricalDescriptivePrice,
    HistoricalSourceMode,
    HistoricalUniverseComplete,
    Phase35TypeConfusionError,
    assert_separate_storage_roots,
    storage_root_for,
)
from weather_alpha.phase35.coverage import (
    CoverageAuditResult,
    CoverageEvidenceDay,
    CoverageStatus,
    audit_historical_coverage,
)
from weather_alpha.phase35.forward_collector import ForwardBookCollector, ForwardCollectTarget
from weather_alpha.phase35.reports import (
    build_forward_collection_audit_report,
    build_forward_executability_report,
    build_historical_calibration_report,
    build_historical_coverage_report,
    build_historical_tail_analysis_report,
    build_phase35_readiness_report,
)
from weather_alpha.research.prices import PricePoint


class ReadinessDimensionValue(StrEnum):
    """Explicit readiness component state. Unknown is never treated as ready."""

    READY = "ready"
    NOT_READY = "not_ready"
    UNKNOWN = "unknown"


class ForwardValidationMode(StrEnum):
    """Distinguishes offline contract validation from a live network smoke."""

    FIXTURE_CONTRACT = "fixture_contract"
    LIVE_SMOKE = "live_smoke"
    NOT_VALIDATED = "not_validated"


def derive_phase35_collection_ready(
    *,
    code_ready: ReadinessDimensionValue,
    historical_source_ready: ReadinessDimensionValue,
    forward_collection_ready: ReadinessDimensionValue,
) -> bool:
    """PHASE35_COLLECTION_READY = CODE ∧ HISTORICAL ∧ FORWARD (executable excluded)."""
    return (
        code_ready is ReadinessDimensionValue.READY
        and historical_source_ready is ReadinessDimensionValue.READY
        and forward_collection_ready is ReadinessDimensionValue.READY
    )


def historical_source_ready_from_coverage_status(
    status: CoverageStatus | str,
) -> ReadinessDimensionValue:
    """Day-grid coverage status alone never grants survivorship-limited readiness.

    Fixture/day-grid ``full_collection_supported`` remains a hard ready signal;
    ``partial`` / blocked stay not-ready (existing fixture partial stays not ready).
    """
    if status == "full_collection_supported":
        return ReadinessDimensionValue.READY
    if status in {"partial", "blocked_no_full_collection"}:
        return ReadinessDimensionValue.NOT_READY
    return ReadinessDimensionValue.UNKNOWN


def historical_source_ready_from_audit(audit: CoverageAuditResult) -> ReadinessDimensionValue:
    return historical_source_ready_from_coverage_status(audit.status)


@dataclass(frozen=True, slots=True)
class HistoricalQualificationInputs:
    """Explicit inputs for survivorship-limited historical research readiness.

    Universe completeness is intentionally NOT a prerequisite. Fixture day-grid
    partial coverage must not satisfy these flags merely by existing events.
    """

    meaningful_point_in_time_window: bool
    point_in_time_integrity: bool
    checkpoint_support: bool
    clob_descriptive_coverage: bool
    meaningful_rediscovery: bool
    gamma_survivorship_limitation_explicit: bool
    no_completeness_claim: bool
    pre_registered_minimums_met: bool
    historical_universe_complete: HistoricalUniverseComplete = HistoricalUniverseComplete.NOT_PROVEN
    max_defensible_historical_window: str | None = None
    gamma_rediscovery_rate: float | None = None
    clob_descriptive_coverage_rate: float | None = None
    ecmwf_grid_total: int | None = None
    ecmwf_grid_success: int | None = None
    ecmwf_grid_failures: int | None = None
    checkpoint_support_map: Mapping[str, bool] | None = None

    def missing_requirements(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.meaningful_point_in_time_window:
            missing.append("meaningful_point_in_time_window")
        if not self.point_in_time_integrity:
            missing.append("point_in_time_integrity")
        if not self.checkpoint_support:
            missing.append("checkpoint_support")
        if not self.clob_descriptive_coverage:
            missing.append("clob_descriptive_coverage")
        if not self.meaningful_rediscovery:
            missing.append("meaningful_rediscovery")
        if not self.gamma_survivorship_limitation_explicit:
            missing.append("gamma_survivorship_limitation_explicit")
        if not self.no_completeness_claim:
            missing.append("no_completeness_claim")
        if not self.pre_registered_minimums_met:
            missing.append("pre_registered_minimums_met")
        if self.historical_universe_complete is HistoricalUniverseComplete.YES:
            # Completeness YES is extraordinary; still allowed, but readiness does not require it.
            pass
        return tuple(missing)


def assess_historical_source_readiness(
    inputs: HistoricalQualificationInputs,
) -> tuple[ReadinessDimensionValue, HistoricalSourceMode, tuple[str, ...]]:
    """HISTORICAL_SOURCE_READY under survivorship_limited_descriptive (universe may be NOT_PROVEN)."""
    missing = inputs.missing_requirements()
    if missing:
        return ReadinessDimensionValue.NOT_READY, HistoricalSourceMode.NOT_ESTABLISHED, missing
    if not inputs.gamma_survivorship_limitation_explicit:
        return (
            ReadinessDimensionValue.NOT_READY,
            HistoricalSourceMode.NOT_ESTABLISHED,
            ("gamma_survivorship_limitation_explicit",),
        )
    if not inputs.no_completeness_claim:
        return (
            ReadinessDimensionValue.NOT_READY,
            HistoricalSourceMode.NOT_ESTABLISHED,
            ("completeness_claim_forbidden_without_proof",),
        )
    return (
        ReadinessDimensionValue.READY,
        HistoricalSourceMode.SURVIVORSHIP_LIMITED_DESCRIPTIVE,
        (),
    )


def forward_collection_ready_from_validation(
    *,
    validation_mode: ForwardValidationMode,
    live_observation_retained: bool,
) -> ReadinessDimensionValue:
    """Fixture-only / not-validated never establishes FORWARD_COLLECTION_READY."""
    if validation_mode is ForwardValidationMode.FIXTURE_CONTRACT:
        return ReadinessDimensionValue.NOT_READY
    if validation_mode is ForwardValidationMode.NOT_VALIDATED:
        return ReadinessDimensionValue.NOT_READY
    if validation_mode is ForwardValidationMode.LIVE_SMOKE and live_observation_retained:
        return ReadinessDimensionValue.READY
    return ReadinessDimensionValue.NOT_READY


def executable_two_sided_book_validated_from_results(
    *,
    validation_mode: ForwardValidationMode,
    executable_flags: Sequence[bool] = (),
) -> bool:
    """YES only after at least one live-accepted two-sided executable book."""
    if validation_mode is not ForwardValidationMode.LIVE_SMOKE:
        return False
    return any(executable_flags)


def count_liquidity_states(
    states: Sequence[str | ForwardLiquidityState],
) -> dict[str, int]:
    counts = {state.value: 0 for state in ForwardLiquidityState}
    for raw in states:
        key = raw.value if isinstance(raw, ForwardLiquidityState) else str(raw)
        if key in counts:
            counts[key] += 1
    return counts


def default_checkpoint_support_placeholders() -> dict[str, bool | None]:
    return {f"{hours}h": None for hours in PRE_REGISTERED_CHECKPOINT_HOURS}


@dataclass(frozen=True, slots=True)
class ReadinessDimensions:
    code_ready: ReadinessDimensionValue
    historical_source_ready: ReadinessDimensionValue
    forward_collection_ready: ReadinessDimensionValue
    forward_validation_mode: ForwardValidationMode = ForwardValidationMode.NOT_VALIDATED
    historical_source_mode: HistoricalSourceMode = HistoricalSourceMode.NOT_ESTABLISHED
    historical_universe_complete: HistoricalUniverseComplete = HistoricalUniverseComplete.NOT_PROVEN
    gamma_survivorship_limitation: bool = True
    max_defensible_historical_window: str | None = None
    gamma_rediscovery_rate: float | None = None
    clob_descriptive_coverage: float | bool | None = None
    ecmwf_grid_total: int | None = None
    ecmwf_grid_success: int | None = None
    ecmwf_grid_failures: int | None = None
    checkpoint_support: Mapping[str, bool | None] = field(
        default_factory=default_checkpoint_support_placeholders
    )
    forward_observations: int = 0
    forward_liquidity_state_counts: Mapping[str, int] = field(default_factory=dict)
    executable_two_sided_book_validated: bool = False

    @property
    def phase35_collection_ready(self) -> bool:
        return derive_phase35_collection_ready(
            code_ready=self.code_ready,
            historical_source_ready=self.historical_source_ready,
            forward_collection_ready=self.forward_collection_ready,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "CHECKPOINT_SUPPORT": dict(self.checkpoint_support),
            "CLOB_DESCRIPTIVE_COVERAGE": self.clob_descriptive_coverage,
            "CODE_READY": self.code_ready.value,
            "ECMWF_GRID_FAILURES": self.ecmwf_grid_failures,
            "ECMWF_GRID_SUCCESS": self.ecmwf_grid_success,
            "ECMWF_GRID_TOTAL": self.ecmwf_grid_total,
            "EXECUTABLE_TWO_SIDED_BOOK_VALIDATED": self.executable_two_sided_book_validated,
            "FORWARD_COLLECTION_READY": self.forward_collection_ready.value,
            "FORWARD_LIQUIDITY_STATE_COUNTS": dict(self.forward_liquidity_state_counts),
            "FORWARD_OBSERVATIONS": self.forward_observations,
            "FORWARD_VALIDATION_MODE": self.forward_validation_mode.value,
            "GAMMA_REDISCOVERY_RATE": self.gamma_rediscovery_rate,
            "GAMMA_SURVIVORSHIP_LIMITATION": self.gamma_survivorship_limitation,
            "HISTORICAL_SOURCE_MODE": self.historical_source_mode.value,
            "HISTORICAL_SOURCE_READY": self.historical_source_ready.value,
            "HISTORICAL_UNIVERSE_COMPLETE": self.historical_universe_complete.value,
            "MAX_DEFENSIBLE_HISTORICAL_WINDOW": self.max_defensible_historical_window,
            "PHASE35_COLLECTION_READY": self.phase35_collection_ready,
            "PHASE35_COLLECTION_READY_NOT_EXECUTABLE_OR_PROFITABLE": True,
        }


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    """Structured Phase 3.5 readiness. Ambiguous collection_ready is not used."""

    dimensions: ReadinessDimensions
    coverage_status: str
    maximum_defensible_span_days: int
    report_paths: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def phase35_collection_ready(self) -> bool:
        return self.dimensions.phase35_collection_ready

    @property
    def code_ready(self) -> ReadinessDimensionValue:
        return self.dimensions.code_ready

    @property
    def historical_source_ready(self) -> ReadinessDimensionValue:
        return self.dimensions.historical_source_ready

    @property
    def forward_collection_ready(self) -> ReadinessDimensionValue:
        return self.dimensions.forward_collection_ready

    def as_dict(self) -> dict[str, Any]:
        payload = self.dimensions.as_dict()
        payload.update(
            {
                "coverage_status": self.coverage_status,
                "full_collection_launched": False,
                "forward_daemon_launched": False,
                "maximum_defensible_span_days": self.maximum_defensible_span_days,
                "notes": list(self.notes),
                "report_paths": list(self.report_paths),
            }
        )
        return payload


class _FixtureBookTransport:
    """In-process GET transport for offline readiness; never touches the network."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = dict(payload)

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> ReadOnlyResponse:
        del headers, timeout
        import json
        from urllib.parse import urlencode, urlsplit, urlunsplit

        split = urlsplit(url)
        items = []
        if params:
            for key, value in params.items():
                items.append((key, str(value)))
        full = urlunsplit((split.scheme, split.netloc, split.path, urlencode(items), ""))
        body = json.dumps(self._payload).encode("utf-8")
        return ReadOnlyResponse(status_code=200, url=full, headers={}, content=body)


def _fixture_evidence_partial_year() -> tuple[CoverageEvidenceDay, ...]:
    """Offline fixture: continuous support for 90 days inside a proposed 365-day window."""
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
                used_stitched_historical_forecast=False,
            )
        )
    return tuple(rows)


def run_offline_readiness(*, output_dir: Path) -> ReadinessResult:
    """Bounded offline readiness: contracts, coverage audit, reports. No full collection."""
    assert_price_bands_registered()
    assert_separate_storage_roots(base=output_dir)
    historical_root = storage_root_for(HISTORICAL_TRACK, base=output_dir)
    forward_root = storage_root_for(FORWARD_TRACK, base=output_dir)
    historical_root.mkdir(parents=True, exist_ok=True)
    forward_root.mkdir(parents=True, exist_ok=True)

    proposed_start = "2025-06-01"
    proposed_end = "2026-05-31"
    audit = audit_historical_coverage(
        proposed_start=proposed_start,
        proposed_end=proposed_end,
        evidence=_fixture_evidence_partial_year(),
    )
    real_schema = audit.to_real_source_audit_schema(audit_mode="fixture_derived")
    coverage_path = build_historical_coverage_report(
        audit,
        output_dir=output_dir,
        real_source_audit=real_schema,
    )

    decision_event = "2026-07-15"
    forecasts = (
        ForecastCandidate(
            issued_at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
            available_at=datetime(2026, 7, 12, 18, 0, tzinfo=UTC),
            run_param="2026-07-12T12:00",
        ),
        ForecastCandidate(
            issued_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            available_at=datetime(2026, 7, 14, 18, 0, tzinfo=UTC),
            run_param="2026-07-14T12:00",
        ),
    )
    prices = (
        PricePoint(observed_at=datetime(2026, 7, 12, 20, 0, tzinfo=UTC), price=0.12),
        PricePoint(observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC), price=0.18),
        PricePoint(observed_at=datetime(2026, 7, 15, 2, 0, tzinfo=UTC), price=0.99),
    )
    selections = select_all_registered_checkpoints(
        event_date=decision_event,
        timezone_name="America/New_York",
        canonical_event_key=("event_id", "fixture-event"),
        forecasts=forecasts,
        prices=prices,
    )
    if registered_checkpoints() != (48, 24, 12, 6, 3, 1):
        raise RuntimeError("checkpoint registry drifted")
    if len(selections) != 6:
        raise RuntimeError("expected six checkpoint selections")

    descriptive = HistoricalDescriptivePrice(
        track=HISTORICAL_TRACK,
        observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
        descriptive_probability=0.18,
        token_id="tok",
        condition_id="cond",
        canonical_event_key=("event_id", "fixture-event"),
    )
    try:
        descriptive.as_executable_price()
    except Phase35TypeConfusionError:
        pass
    else:
        raise RuntimeError("historical descriptive price must not expose executable conversion")

    _ = build_stratification(
        city="new york",
        station="KLGA",
        event_date=decision_event,
        lead_hours=24,
        bucket_kind="exact",
        descriptive_probability=0.18,
        model_probability=0.22,
        raw_edge=0.04,
    )

    groups = tuple(
        EventGroupMetric(
            canonical_event_key=("event_id", f"e{i}"),
            event_date=f"2026-{((i % 6) + 1):02d}-{((i % 20) + 1):02d}",
            city=["paris", "london", "new york", "munich"][i % 4],
            station=["LFPG", "EGLC", "KLGA", "EDDM"][i % 4],
            lead_hours=[48, 24, 12, 6, 3, 1][i % 6],
            metric=0.1 + (i % 7) * 0.01,
            favorable=i % 11 == 0,
            is_tail_winner=i % 13 == 0,
        )
        for i in range(40)
    )
    acceptance = assess_acceptance_thresholds(groups, interpreted_leads=(24, 12))
    bootstrap = blocked_bootstrap_mean_ci(groups, n_boot=50, seed=7)
    robustness = robustness_report(groups)
    cal_path = build_historical_calibration_report(
        acceptance=acceptance,
        bootstrap=bootstrap,
        output_dir=output_dir,
        measured={"checkpoint_selection_statuses": [row.status for row in selections]},
    )
    tail_path = build_historical_tail_analysis_report(robustness=robustness, output_dir=output_dir)

    book_payload = {
        "market": "cond-fixture",
        "asset_id": "tok-fixture",
        "timestamp": int(datetime(2026, 7, 14, 12, 0, tzinfo=UTC).timestamp()),
        "bids": [{"price": "0.40", "size": "100"}, {"price": "0.39", "size": "200"}],
        "asks": [{"price": "0.42", "size": "80"}, {"price": "0.43", "size": "120"}],
    }
    book = validate_order_book(book_payload)
    fills = hypothetical_fills_for_config(book, config=Phase35ForwardConfig())
    exec_path = build_forward_executability_report(book=book, fills=fills, output_dir=output_dir)

    collector = ForwardBookCollector(
        ReadOnlyHttpClient(transport=_FixtureBookTransport(book_payload), max_retries=0),
        output_root=forward_root,
        retrieved_at=datetime(2026, 7, 14, 12, 5, tzinfo=UTC),
    )
    result = collector.collect_one(
        ForwardCollectTarget(
            canonical_event_id="fixture-event",
            condition_id="cond-fixture",
            market_id="m-fixture",
            token_id="tok-fixture",
            city="new york",
            station_icao="KLGA",
            event_date=decision_event,
            native_unit="fahrenheit",
            bucket_definition="80-81F",
            decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            checkpoint_lead_hours=24,
            model_probability=0.22,
            descriptive_market_probability=0.18,
        )
    )
    snapshots = []
    quarantined = 0
    forward_contract_ok = False
    liquidity_states: list[str] = []
    if result.snapshot is not None and result.quarantined is False:
        snapshots.append(result.snapshot)
        forward_contract_ok = True
        if result.liquidity_state is not None:
            liquidity_states.append(result.liquidity_state)
    else:
        quarantined += 1
        if result.liquidity_state is not None:
            liquidity_states.append(result.liquidity_state)
    forward_mode = ForwardValidationMode.FIXTURE_CONTRACT
    audit_path = build_forward_collection_audit_report(
        snapshots=tuple(snapshots),
        quarantined=quarantined,
        output_dir=output_dir,
        validation_mode=forward_mode.value,
        liquidity_state_counts=count_liquidity_states(liquidity_states),
        executable_two_sided_book_validated=False,
    )

    historical_dim = historical_source_ready_from_audit(audit)
    if acceptance.alpha_claimed:
        code_dim = ReadinessDimensionValue.NOT_READY
        code_note = "CODE_READY=false because acceptance incorrectly claimed alpha"
    else:
        code_dim = ReadinessDimensionValue.READY
        code_note = "CODE_READY=true: offline contracts/checkpoints/reports exercised"
    # Fixture-only must never establish forward live readiness.
    forward_dim = forward_collection_ready_from_validation(
        validation_mode=forward_mode,
        live_observation_retained=forward_contract_ok,
    )
    if forward_contract_ok:
        forward_note = (
            "FORWARD_COLLECTION_READY=false: fixture_contract validation only; "
            "not a live smoke and never establishes live forward readiness"
        )
    else:
        forward_note = "FORWARD_COLLECTION_READY=false: offline collector contract failed"

    window_label = (
        f"{audit.maximum_defensible_contiguous_start}..{audit.maximum_defensible_contiguous_end}"
        if audit.maximum_defensible_contiguous_start and audit.maximum_defensible_contiguous_end
        else None
    )
    dimensions = ReadinessDimensions(
        code_ready=code_dim,
        historical_source_ready=historical_dim,
        forward_collection_ready=forward_dim,
        forward_validation_mode=forward_mode,
        historical_source_mode=HistoricalSourceMode.NOT_ESTABLISHED,
        historical_universe_complete=HistoricalUniverseComplete.NOT_PROVEN,
        gamma_survivorship_limitation=True,
        max_defensible_historical_window=window_label,
        gamma_rediscovery_rate=None,
        clob_descriptive_coverage=None,
        ecmwf_grid_total=None,
        ecmwf_grid_success=None,
        ecmwf_grid_failures=None,
        checkpoint_support=default_checkpoint_support_placeholders(),
        forward_observations=len(snapshots),
        forward_liquidity_state_counts=count_liquidity_states(liquidity_states),
        executable_two_sided_book_validated=False,
    )
    notes = (
        "Phase 3.5 readiness only: no full 12-month collection and no forward daemon.",
        "PHASE35_COLLECTION_READY does not imply strategy executable or profitable.",
        f"coverage_status={audit.status}",
        f"max_defensible_span_days={audit.maximum_defensible_span_days}",
        "historical descriptive and forward executable namespaces remain separate",
        "HISTORICAL_UNIVERSE_COMPLETE=not_proven; universe completeness is not readiness",
        code_note,
        (
            "HISTORICAL_SOURCE_READY=false for fixture partial coverage; "
            "code-ready does not imply sources ready"
            if historical_dim is not ReadinessDimensionValue.READY
            else "HISTORICAL_SOURCE_READY=true"
        ),
        forward_note,
        (
            "EXECUTABLE_TWO_SIDED_BOOK_VALIDATED=false "
            "(fixture path; live two-sided validation is separate)"
        ),
        f"PHASE35_COLLECTION_READY={dimensions.phase35_collection_ready}",
        f"book_executable_two_sided_fixture={is_executable_two_sided_book(book)}",
    )
    readiness_path = build_phase35_readiness_report(
        dimensions=dimensions,
        coverage_status=audit.status,
        maximum_defensible_span_days=audit.maximum_defensible_span_days,
        real_source_audit=real_schema,
        output_dir=output_dir,
        notes=notes,
    )
    return ReadinessResult(
        dimensions=dimensions,
        coverage_status=audit.status,
        maximum_defensible_span_days=audit.maximum_defensible_span_days,
        report_paths=(
            str(coverage_path),
            str(cal_path),
            str(tail_path),
            str(audit_path),
            str(exec_path),
            str(readiness_path),
        ),
        notes=notes,
    )
