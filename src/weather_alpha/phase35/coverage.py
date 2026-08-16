"""Historical source coverage audit for proposed Phase 3.5 expansion.

Fixture-testable and network-free by default. Does not silently substitute
stitched retrospective forecast products for point-in-time Single Runs.

Real provider probe results use RealSourceCoverageAuditResult; Hermes may fill
that schema later. This module never hardcodes a 12-month pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any, Literal

from weather_alpha.config.settings import parse_iso_date
from weather_alpha.phase35.config import ECMWF_SINGLE_RUNS_EARLIEST_DEFENSIBLE

CoverageStatus = Literal["full_collection_supported", "partial", "blocked_no_full_collection"]

REQUIRED_EVIDENCE_FIELDS: tuple[str, ...] = (
    "identity",
    "settlement",
    "price",
    "ecmwf_single_runs_init",
    "ecmwf_single_runs_availability",
    "station",
    "event_date",
    "unit",
    "provenance",
)


class ProviderCoverageStatus(StrEnum):
    """Machine-readable per-provider coverage status for real or fixture audits."""

    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    NOT_AUDITED = "not_audited"


@dataclass(frozen=True, slots=True)
class CoverageEvidenceDay:
    """Per-day explicit coverage flags. Missing/false is not silently filled."""

    day: str
    identity: bool = False
    settlement: bool = False
    price: bool = False
    ecmwf_single_runs_init: bool = False
    ecmwf_single_runs_availability: bool = False
    station: bool = False
    event_date: bool = False
    unit: bool = False
    provenance: bool = False
    used_stitched_historical_forecast: bool = False
    notes: tuple[str, ...] = ()

    def fully_supported(self) -> bool:
        if self.used_stitched_historical_forecast:
            return False
        return all(
            (
                self.identity,
                self.settlement,
                self.price,
                self.ecmwf_single_runs_init,
                self.ecmwf_single_runs_availability,
                self.station,
                self.event_date,
                self.unit,
                self.provenance,
            )
        )


@dataclass(frozen=True, slots=True)
class DateWindow:
    """Inclusive calendar window; nulls mean unknown/unavailable."""

    start: str | None
    end: str | None
    span_days: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "end": self.end,
            "span_days": self.span_days,
            "start": self.start,
        }


@dataclass(frozen=True, slots=True)
class MissingInterval:
    start: str
    end: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "end": self.end,
            "reason": self.reason,
            "start": self.start,
        }


@dataclass(frozen=True, slots=True)
class ProviderCoverageEvidence:
    provider: str
    status: ProviderCoverageStatus
    evidence: dict[str, Any]
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence": dict(self.evidence),
            "notes": list(self.notes),
            "provider": self.provider,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class PointInTimeIntegrity:
    """Whether audit evidence preserves point-in-time forecast constraints."""

    ecmwf_single_runs_only: bool
    no_stitched_historical_forecast_substitution: bool
    available_at_respected: bool | None
    integrity_status: Literal["preserved", "violated", "unknown", "not_audited"]
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "available_at_respected": self.available_at_respected,
            "ecmwf_single_runs_only": self.ecmwf_single_runs_only,
            "integrity_status": self.integrity_status,
            "no_stitched_historical_forecast_substitution": (
                self.no_stitched_historical_forecast_substitution
            ),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class RealSourceCoverageAuditResult:
    """Provider-based historical coverage audit schema (network fill deferred).

    Does not assert a 12-month pass. historical_source_ready is true only when
    the requested window is fully supported by real/provider evidence.
    """

    requested_historical_window: DateWindow
    maximum_defensible_contiguous_window: DateWindow
    ecmwf_single_runs_coverage: ProviderCoverageEvidence
    gamma_discovery_coverage: ProviderCoverageEvidence
    clob_price_history_coverage: ProviderCoverageEvidence
    known_gaps: tuple[MissingInterval, ...]
    survivorship_limitations: tuple[str, ...]
    point_in_time_integrity: PointInTimeIntegrity
    historical_source_ready: bool
    audit_mode: Literal["real_provider", "fixture_derived", "schema_only"] = "schema_only"
    coverage_status: CoverageStatus | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "audit_mode": self.audit_mode,
            "clob_price_history_coverage": self.clob_price_history_coverage.as_dict(),
            "coverage_status": self.coverage_status,
            "ecmwf_single_runs_coverage": self.ecmwf_single_runs_coverage.as_dict(),
            "gamma_discovery_coverage": self.gamma_discovery_coverage.as_dict(),
            "historical_source_ready": self.historical_source_ready,
            "known_gaps": [gap.as_dict() for gap in self.known_gaps],
            "maximum_defensible_contiguous_window": (
                self.maximum_defensible_contiguous_window.as_dict()
            ),
            "point_in_time_integrity": self.point_in_time_integrity.as_dict(),
            "requested_historical_window": self.requested_historical_window.as_dict(),
            "survivorship_limitations": list(self.survivorship_limitations),
        }


@dataclass(frozen=True, slots=True)
class CoverageAuditResult:
    status: CoverageStatus
    proposed_start: str
    proposed_end: str
    proposed_span_days: int
    maximum_defensible_contiguous_start: str | None
    maximum_defensible_contiguous_end: str | None
    maximum_defensible_span_days: int
    blocked_reasons: tuple[str, ...]
    evidence_summary: dict[str, Any]
    descriptive_only: bool = True
    full_collection_authorized: bool = False
    missing_intervals: tuple[MissingInterval, ...] = ()

    @property
    def historical_source_ready(self) -> bool:
        """True only for continuous full-window day-grid support.

        Distinct from survivorship-limited research readiness
        (``assess_historical_source_readiness``), which may be ready with
        ``HISTORICAL_UNIVERSE_COMPLETE=not_proven``. Fixture partial stays false.
        """
        return self.status == "full_collection_supported"

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocked_reasons": list(self.blocked_reasons),
            "descriptive_only": self.descriptive_only,
            "evidence_summary": self.evidence_summary,
            "full_collection_authorized": self.full_collection_authorized,
            "historical_source_ready": self.historical_source_ready,
            "maximum_defensible_contiguous_end": self.maximum_defensible_contiguous_end,
            "maximum_defensible_contiguous_start": self.maximum_defensible_contiguous_start,
            "maximum_defensible_span_days": self.maximum_defensible_span_days,
            "missing_intervals": [gap.as_dict() for gap in self.missing_intervals],
            "proposed_end": self.proposed_end,
            "proposed_span_days": self.proposed_span_days,
            "proposed_start": self.proposed_start,
            "status": self.status,
        }

    def to_real_source_audit_schema(
        self,
        *,
        audit_mode: Literal["real_provider", "fixture_derived", "schema_only"] = "fixture_derived",
        ecmwf: ProviderCoverageEvidence | None = None,
        gamma: ProviderCoverageEvidence | None = None,
        clob: ProviderCoverageEvidence | None = None,
        survivorship_limitations: tuple[str, ...] | None = None,
        point_in_time_integrity: PointInTimeIntegrity | None = None,
    ) -> RealSourceCoverageAuditResult:
        """Project fixture/day-grid audit into the real-provider report schema."""
        default_notes: tuple[str, ...] = (
            ("Projected from day-grid CoverageAuditResult; not a live provider probe.",)
            if audit_mode != "real_provider"
            else ()
        )
        not_audited = ProviderCoverageStatus.NOT_AUDITED
        ecmwf_ev = ecmwf or ProviderCoverageEvidence(
            provider="open_meteo_ecmwf_ifs_single_runs",
            status=not_audited,
            evidence={"source": "coverage_day_grid", "status": self.status},
            notes=default_notes,
        )
        gamma_ev = gamma or ProviderCoverageEvidence(
            provider="polymarket_gamma_public_search",
            status=not_audited,
            evidence={"source": "coverage_day_grid", "identity_field_failures": True},
            notes=(
                *default_notes,
                "Gamma public-search is a current index; survivorship may omit delisted markets.",
            ),
        )
        clob_ev = clob or ProviderCoverageEvidence(
            provider="polymarket_clob_prices_history",
            status=not_audited,
            evidence={
                "source": "coverage_day_grid",
                "descriptive_only": True,
                "not_executable": True,
            },
            notes=default_notes,
        )
        integrity = point_in_time_integrity or PointInTimeIntegrity(
            ecmwf_single_runs_only=True,
            no_stitched_historical_forecast_substitution=True,
            available_at_respected=None,
            integrity_status="not_audited" if audit_mode != "real_provider" else "unknown",
            notes=default_notes,
        )
        surv = survivorship_limitations or (
            "Gamma public-search survivorship: historical event universe completeness "
            "is not claimed from fixture day-grid evidence.",
        )
        return RealSourceCoverageAuditResult(
            requested_historical_window=DateWindow(
                start=self.proposed_start,
                end=self.proposed_end,
                span_days=self.proposed_span_days,
            ),
            maximum_defensible_contiguous_window=DateWindow(
                start=self.maximum_defensible_contiguous_start,
                end=self.maximum_defensible_contiguous_end,
                span_days=self.maximum_defensible_span_days,
            ),
            ecmwf_single_runs_coverage=ecmwf_ev,
            gamma_discovery_coverage=gamma_ev,
            clob_price_history_coverage=clob_ev,
            known_gaps=self.missing_intervals,
            survivorship_limitations=surv,
            point_in_time_integrity=integrity,
            historical_source_ready=self.historical_source_ready,
            audit_mode=audit_mode,
            coverage_status=self.status,
        )


def _daterange(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _longest_contiguous_supported(
    days: list[CoverageEvidenceDay],
) -> tuple[str | None, str | None, int]:
    best_start: str | None = None
    best_end: str | None = None
    best_len = 0
    run_start: str | None = None
    run_end: str | None = None
    run_len = 0
    for row in days:
        if row.fully_supported():
            if run_start is None:
                run_start = row.day
            run_end = row.day
            run_len += 1
            if run_len > best_len:
                best_len = run_len
                best_start = run_start
                best_end = run_end
        else:
            run_start = None
            run_end = None
            run_len = 0
    return best_start, best_end, best_len


def _missing_intervals(days: list[CoverageEvidenceDay]) -> tuple[MissingInterval, ...]:
    intervals: list[MissingInterval] = []
    gap_start: str | None = None
    gap_end: str | None = None
    for row in days:
        if row.fully_supported():
            if gap_start is not None and gap_end is not None:
                intervals.append(
                    MissingInterval(
                        start=gap_start,
                        end=gap_end,
                        reason="unsupported_or_missing_evidence",
                    )
                )
            gap_start = None
            gap_end = None
            continue
        if gap_start is None:
            gap_start = row.day
        gap_end = row.day
    if gap_start is not None and gap_end is not None:
        intervals.append(
            MissingInterval(
                start=gap_start,
                end=gap_end,
                reason="unsupported_or_missing_evidence",
            )
        )
    return tuple(intervals)


def audit_historical_coverage(
    *,
    proposed_start: str,
    proposed_end: str,
    evidence: tuple[CoverageEvidenceDay, ...] | list[CoverageEvidenceDay],
    ecmwf_earliest_defensible: str = ECMWF_SINGLE_RUNS_EARLIEST_DEFENSIBLE,
    require_full_year_days: int = 365,
) -> CoverageAuditResult:
    start = parse_iso_date(proposed_start)
    end = parse_iso_date(proposed_end)
    if end < start:
        raise ValueError("proposed_end must be on or after proposed_start")
    span_days = (end - start).days + 1
    by_day = {row.day: row for row in evidence}
    ordered_days = [day.isoformat() for day in _daterange(start, end)]
    filled: list[CoverageEvidenceDay] = []
    missing_days = 0
    stitched = 0
    field_failures: dict[str, int] = {name: 0 for name in REQUIRED_EVIDENCE_FIELDS}
    blocked: list[str] = []

    ecmwf_floor = parse_iso_date(ecmwf_earliest_defensible)
    if start < ecmwf_floor:
        blocked.append(
            f"proposed_start {proposed_start} precedes ECMWF Single Runs defensible floor "
            f"{ecmwf_earliest_defensible}; Open-Meteo Historical Forecast must not substitute"
        )

    for day in ordered_days:
        row = by_day.get(day)
        if row is None:
            missing_days += 1
            filled.append(CoverageEvidenceDay(day=day))
            for name in REQUIRED_EVIDENCE_FIELDS:
                field_failures[name] += 1
            continue
        if row.used_stitched_historical_forecast:
            stitched += 1
            blocked.append(
                f"{day}: stitched retrospective Historical Forecast substitution is forbidden"
            )
        for name in REQUIRED_EVIDENCE_FIELDS:
            if not bool(getattr(row, name)):
                field_failures[name] += 1
        filled.append(row)

    max_start, max_end, max_span = _longest_contiguous_supported(filled)
    missing_intervals = _missing_intervals(filled)
    if missing_days:
        blocked.append(f"missing_coverage_evidence_days={missing_days}")
    for name, count in field_failures.items():
        if count:
            blocked.append(f"unsupported_{name}_days={count}")

    evidence_summary = {
        "ecmwf_earliest_defensible": ecmwf_earliest_defensible,
        "field_failure_days": field_failures,
        "forbidden_stitched_substitution_days": stitched,
        "missing_evidence_days": missing_days,
        "required_evidence_fields": list(REQUIRED_EVIDENCE_FIELDS),
        "supported_days": sum(1 for row in filled if row.fully_supported()),
        "total_proposed_days": span_days,
    }

    unique_blocked = tuple(dict.fromkeys(blocked))
    full_ok = (
        span_days >= require_full_year_days
        and max_span >= require_full_year_days
        and max_start == proposed_start
        and max_end == proposed_end
        and stitched == 0
        and missing_days == 0
        and start >= ecmwf_floor
    )
    if full_ok:
        status: CoverageStatus = "full_collection_supported"
        return CoverageAuditResult(
            status=status,
            proposed_start=proposed_start,
            proposed_end=proposed_end,
            proposed_span_days=span_days,
            maximum_defensible_contiguous_start=max_start,
            maximum_defensible_contiguous_end=max_end,
            maximum_defensible_span_days=max_span,
            blocked_reasons=(),
            evidence_summary=evidence_summary,
            full_collection_authorized=False,  # readiness pass does not authorize collection
            missing_intervals=(),
        )

    status = "partial" if max_span > 0 else "blocked_no_full_collection"
    if span_days >= require_full_year_days and max_span < require_full_year_days:
        unique_blocked = (
            *unique_blocked,
            "twelve_month_continuous_coverage_unsupported",
        )
    return CoverageAuditResult(
        status=status,
        proposed_start=proposed_start,
        proposed_end=proposed_end,
        proposed_span_days=span_days,
        maximum_defensible_contiguous_start=max_start,
        maximum_defensible_contiguous_end=max_end,
        maximum_defensible_span_days=max_span,
        blocked_reasons=unique_blocked,
        evidence_summary=evidence_summary,
        full_collection_authorized=False,
        missing_intervals=missing_intervals,
    )


def build_real_source_coverage_audit(
    *,
    requested_historical_window: DateWindow,
    maximum_defensible_contiguous_window: DateWindow,
    ecmwf_single_runs_coverage: ProviderCoverageEvidence,
    gamma_discovery_coverage: ProviderCoverageEvidence,
    clob_price_history_coverage: ProviderCoverageEvidence,
    known_gaps: tuple[MissingInterval, ...] | list[MissingInterval],
    survivorship_limitations: tuple[str, ...] | list[str],
    point_in_time_integrity: PointInTimeIntegrity,
    coverage_status: CoverageStatus | None = None,
    audit_mode: Literal["real_provider", "fixture_derived", "schema_only"] = "real_provider",
) -> RealSourceCoverageAuditResult:
    """Construct a real-provider audit result without implying a 12-month pass.

    historical_source_ready is true only when every provider is SUPPORTED and the
    maximum defensible window fully covers the requested window.
    """
    gaps = tuple(known_gaps)
    requested_span = requested_historical_window.span_days
    max_span = maximum_defensible_contiguous_window.span_days
    windows_align = (
        requested_historical_window.start is not None
        and requested_historical_window.end is not None
        and requested_historical_window.start == maximum_defensible_contiguous_window.start
        and requested_historical_window.end == maximum_defensible_contiguous_window.end
        and requested_span is not None
        and max_span is not None
        and max_span >= requested_span
    )
    providers = (
        ecmwf_single_runs_coverage,
        gamma_discovery_coverage,
        clob_price_history_coverage,
    )
    providers_ok = all(row.status is ProviderCoverageStatus.SUPPORTED for row in providers)
    integrity_ok = point_in_time_integrity.integrity_status == "preserved"
    historical_source_ready = bool(windows_align and providers_ok and integrity_ok and not gaps)
    return RealSourceCoverageAuditResult(
        requested_historical_window=requested_historical_window,
        maximum_defensible_contiguous_window=maximum_defensible_contiguous_window,
        ecmwf_single_runs_coverage=ecmwf_single_runs_coverage,
        gamma_discovery_coverage=gamma_discovery_coverage,
        clob_price_history_coverage=clob_price_history_coverage,
        known_gaps=gaps,
        survivorship_limitations=tuple(survivorship_limitations),
        point_in_time_integrity=point_in_time_integrity,
        historical_source_ready=historical_source_ready,
        audit_mode=audit_mode,
        coverage_status=coverage_status,
    )
