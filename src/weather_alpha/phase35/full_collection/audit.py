"""Deterministic PHASE35_DATASET_READY audit. Offline; does not collect."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from weather_alpha.phase35.checkpoints import select_checkpoint_inputs
from weather_alpha.phase35.full_collection.ledger import ResultClassification
from weather_alpha.phase35.full_collection.policy import (
    CITY_COVERAGE_MIN,
    CLOB_CITY_MIN,
    CLOB_OVERALL_MIN,
    FUTURE_LEAKAGE_MAX,
    LEAD_COVERAGE_MIN,
    MAX_UNRESOLVED_SYSTEMATIC_CLUSTERS,
    MONTH_COVERAGE_MIN,
    OVERALL_COVERAGE_MIN,
    RAW_HASH_FAILURE_MAX,
    RETROSPECTIVE_SUBSTITUTION_MAX,
    SETTLEMENT_OVERALL_MIN,
    SETTLEMENT_SCORED_MIN,
    SYSTEMATIC_CLUSTER_CONSECUTIVE_DAYS,
    UNREVIEWED_INVALID_GROUP_MAX,
)
from weather_alpha.phase35.full_collection.provenance import assert_text_has_no_machine_roots
from weather_alpha.research.prices import PricePoint
from weather_alpha.research.reports import render_markdown, research_contract


@dataclass(frozen=True, slots=True)
class ExpectedCell:
    date: str
    city: str
    station: str
    checkpoint: int
    event_family_id: str
    month: str
    ecmwf_run_cycle: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkpoint": self.checkpoint,
            "city": self.city,
            "date": self.date,
            "ecmwf_run_cycle": self.ecmwf_run_cycle,
            "event_family_id": self.event_family_id,
            "month": self.month,
            "station": self.station,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExpectedCell:
        cycle = payload.get("ecmwf_run_cycle")
        return cls(
            date=str(payload["date"]),
            city=str(payload["city"]),
            station=str(payload["station"]),
            checkpoint=int(payload["checkpoint"]),
            event_family_id=str(payload["event_family_id"]),
            month=str(payload["month"]),
            ecmwf_run_cycle=None if cycle is None else str(cycle),
        )


@dataclass(frozen=True, slots=True)
class DatasetObservation:
    date: str
    city: str
    station: str
    checkpoint: int
    event_family_id: str
    month: str
    ecmwf_run_cycle: str | None
    observed: bool
    usable: bool
    has_settlement: bool
    scored: bool
    has_price_history: bool
    future_leakage: bool
    retrospective_substitution: bool
    raw_hash_ok: bool
    topology_valid: bool
    topology_reviewed_quarantine: bool
    missing_reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkpoint": self.checkpoint,
            "city": self.city,
            "date": self.date,
            "ecmwf_run_cycle": self.ecmwf_run_cycle,
            "event_family_id": self.event_family_id,
            "future_leakage": self.future_leakage,
            "has_price_history": self.has_price_history,
            "has_settlement": self.has_settlement,
            "missing_reasons": list(self.missing_reasons),
            "month": self.month,
            "observed": self.observed,
            "raw_hash_ok": self.raw_hash_ok,
            "retrospective_substitution": self.retrospective_substitution,
            "scored": self.scored,
            "station": self.station,
            "topology_reviewed_quarantine": self.topology_reviewed_quarantine,
            "topology_valid": self.topology_valid,
            "usable": self.usable,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DatasetObservation:
        cycle = payload.get("ecmwf_run_cycle")
        reasons = payload.get("missing_reasons") or ()
        return cls(
            date=str(payload["date"]),
            city=str(payload["city"]),
            station=str(payload["station"]),
            checkpoint=int(payload["checkpoint"]),
            event_family_id=str(payload["event_family_id"]),
            month=str(payload["month"]),
            ecmwf_run_cycle=None if cycle is None else str(cycle),
            observed=bool(payload["observed"]),
            usable=bool(payload["usable"]),
            has_settlement=bool(payload["has_settlement"]),
            scored=bool(payload["scored"]),
            has_price_history=bool(payload["has_price_history"]),
            future_leakage=bool(payload["future_leakage"]),
            retrospective_substitution=bool(payload["retrospective_substitution"]),
            raw_hash_ok=bool(payload["raw_hash_ok"]),
            topology_valid=bool(payload["topology_valid"]),
            topology_reviewed_quarantine=bool(payload["topology_reviewed_quarantine"]),
            missing_reasons=tuple(str(item) for item in reasons),
        )


@dataclass(frozen=True, slots=True)
class CompletenessCell:
    key: str
    expected_count: int
    observed_count: int
    usable_count: int
    missing_count: int
    missing_fraction: float
    missing_reasons: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected_count": self.expected_count,
            "key": self.key,
            "missing_count": self.missing_count,
            "missing_fraction": self.missing_fraction,
            "missing_reasons": dict(self.missing_reasons),
            "observed_count": self.observed_count,
            "usable_count": self.usable_count,
        }


@dataclass(frozen=True, slots=True)
class DatasetAuditResult:
    phase35_dataset_ready: bool
    provenance_complete: bool
    point_in_time_integrity: bool
    topology_integrity: bool
    settlement_availability_acceptable: bool
    descriptive_price_coverage_acceptable: bool
    coverage_policy_accepted: bool
    no_unresolved_systematic_failure_cluster: bool
    future_leakage_count: int
    retrospective_substitution_count: int
    raw_provenance_hash_failures: int
    invalid_event_group_count: int
    matrices: dict[str, tuple[CompletenessCell, ...]]
    blocked_reasons: tuple[str, ...]
    systematic_clusters: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "PHASE35_DATASET_READY": self.phase35_dataset_ready,
            "blocked_reasons": list(self.blocked_reasons),
            "coverage_policy_accepted": self.coverage_policy_accepted,
            "descriptive_price_coverage_acceptable": self.descriptive_price_coverage_acceptable,
            "future_leakage_count": self.future_leakage_count,
            "invalid_event_group_count": self.invalid_event_group_count,
            "matrices": {
                name: [cell.as_dict() for cell in cells] for name, cells in self.matrices.items()
            },
            "no_unresolved_systematic_failure_cluster": (
                self.no_unresolved_systematic_failure_cluster
            ),
            "point_in_time_integrity": self.point_in_time_integrity,
            "provenance_complete": self.provenance_complete,
            "raw_provenance_hash_failures": self.raw_provenance_hash_failures,
            "retrospective_substitution_count": self.retrospective_substitution_count,
            "settlement_availability_acceptable": self.settlement_availability_acceptable,
            "systematic_clusters": list(self.systematic_clusters),
            "topology_integrity": self.topology_integrity,
        }
        encoded = str(payload)
        assert_text_has_no_machine_roots(encoded)
        return payload


def _fraction(missing: int, expected: int) -> float:
    if expected <= 0:
        return 0.0
    return missing / expected


def _cell(
    key: str, expected: int, observed: int, usable: int, reasons: dict[str, int]
) -> CompletenessCell:
    missing = expected - observed
    if missing < 0:
        missing = 0
    return CompletenessCell(
        key=key,
        expected_count=expected,
        observed_count=observed,
        usable_count=usable,
        missing_count=missing,
        missing_fraction=_fraction(missing, expected),
        missing_reasons=dict(sorted(reasons.items())),
    )


def _group_matrix(
    expected: tuple[ExpectedCell, ...],
    observations: dict[tuple[str, str, str, int, str], DatasetObservation],
    key_fn: Any,
) -> tuple[CompletenessCell, ...]:
    buckets: dict[str, list[ExpectedCell]] = defaultdict(list)
    for cell in expected:
        buckets[str(key_fn(cell))].append(cell)
    out: list[CompletenessCell] = []
    for key in sorted(buckets):
        rows = buckets[key]
        observed = 0
        usable = 0
        reasons: dict[str, int] = defaultdict(int)
        for cell in rows:
            obs = observations.get(
                (cell.date, cell.city, cell.station, cell.checkpoint, cell.event_family_id)
            )
            if obs is not None and obs.observed:
                observed += 1
                if obs.usable:
                    usable += 1
                for reason in obs.missing_reasons:
                    reasons[reason] += 1
            else:
                reasons["missing"] += 1
                if obs is not None:
                    for reason in obs.missing_reasons:
                        reasons[reason] += 1
        out.append(_cell(key, len(rows), observed, usable, dict(reasons)))
    return tuple(out)


def _coverage(cell: CompletenessCell) -> float:
    if cell.expected_count <= 0:
        return 1.0
    return cell.usable_count / cell.expected_count


def _consecutive_runs(days: list[str], threshold: int) -> list[tuple[str, str, int]]:
    if not days:
        return []
    ordered = sorted(date.fromisoformat(day) for day in days)
    runs: list[tuple[str, str, int]] = []
    start = ordered[0]
    prev = ordered[0]
    length = 1
    for current in ordered[1:]:
        if current == prev + timedelta(days=1):
            length += 1
            prev = current
            continue
        if length >= threshold:
            runs.append((start.isoformat(), prev.isoformat(), length))
        start = current
        prev = current
        length = 1
    if length >= threshold:
        runs.append((start.isoformat(), prev.isoformat(), length))
    return runs


def audit_dataset(
    *,
    expected: tuple[ExpectedCell, ...] | list[ExpectedCell],
    observations: tuple[DatasetObservation, ...] | list[DatasetObservation],
    ledger_classifications: tuple[ResultClassification, ...] = (),
) -> DatasetAuditResult:
    expected_t = tuple(expected)
    obs_index = {
        (row.date, row.city, row.station, row.checkpoint, row.event_family_id): row
        for row in observations
    }
    dimensions = {
        "DATE": lambda cell: cell.date,
        "CITY": lambda cell: cell.city,
        "STATION": lambda cell: cell.station,
        "CHECKPOINT": lambda cell: cell.checkpoint,
        "ECMWF_RUN_CYCLE": lambda cell: cell.ecmwf_run_cycle or "missing",
        "EVENT_FAMILY": lambda cell: cell.event_family_id,
        "PRICE_HISTORY": lambda cell: f"{cell.city}:{cell.date}",
        "MONTH": lambda cell: cell.month,
    }
    matrices = {name: _group_matrix(expected_t, obs_index, fn) for name, fn in dimensions.items()}

    future = sum(1 for row in observations if row.future_leakage)
    retrospective = sum(1 for row in observations if row.retrospective_substitution)
    hash_failures = sum(1 for row in observations if row.observed and not row.raw_hash_ok)
    invalid_groups = len(
        {
            row.event_family_id
            for row in observations
            if not row.topology_valid and not row.topology_reviewed_quarantine
        }
    )

    overall = _cell(
        "OVERALL",
        len(expected_t),
        sum(1 for row in observations if row.observed),
        sum(1 for row in observations if row.usable),
        {},
    )
    city_ok = all(_coverage(cell) >= CITY_COVERAGE_MIN for cell in matrices["CITY"])
    lead_ok = all(_coverage(cell) >= LEAD_COVERAGE_MIN for cell in matrices["CHECKPOINT"])
    month_ok = all(_coverage(cell) >= MONTH_COVERAGE_MIN for cell in matrices["MONTH"])
    overall_ok = _coverage(overall) >= OVERALL_COVERAGE_MIN
    if not expected_t:
        city_ok = False
        lead_ok = False
        month_ok = False
        overall_ok = False

    settlement_expected = len({cell.event_family_id for cell in expected_t})
    settled = len({row.event_family_id for row in observations if row.has_settlement})
    scored = [row for row in observations if row.scored]
    scored_settled = all(row.has_settlement for row in scored) if scored else True
    settlement_overall = 1.0 if settlement_expected == 0 else settled / settlement_expected
    settlement_ok = (
        bool(expected_t)
        and settlement_overall >= SETTLEMENT_OVERALL_MIN
        and (
            (sum(1 for row in scored if row.has_settlement) / len(scored) if scored else 1.0)
            >= SETTLEMENT_SCORED_MIN
            and scored_settled
        )
    )

    price_expected = len({(cell.city, cell.date) for cell in expected_t})
    price_obs = len({(row.city, row.date) for row in observations if row.has_price_history})
    clob_overall = 1.0 if price_expected == 0 else price_obs / price_expected
    city_price_ok = True
    cities = sorted({cell.city for cell in expected_t})
    for city in cities:
        exp = len({cell.date for cell in expected_t if cell.city == city})
        got = len({row.date for row in observations if row.city == city and row.has_price_history})
        frac = 1.0 if exp == 0 else got / exp
        if frac < CLOB_CITY_MIN:
            city_price_ok = False
    clob_ok = bool(expected_t) and clob_overall >= CLOB_OVERALL_MIN and city_price_ok

    failure_days: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in observations:
        operational = any(
            reason
            in {
                "RATE_LIMITED",
                "TIMEOUT",
                "TLS_FAILURE",
                "HTTP_FAILURE",
                "TRANSIENT_TRANSPORT_FAILURE",
                "TRANSIENT_5XX",
                "INTERRUPTED_RESUMABLE",
                "retry_reserve_exhausted",
                "retry_exhausted",
                "global_get_attempts",
            }
            or reason.startswith("operational:")
            for reason in row.missing_reasons
        )
        if operational or (
            not row.usable
            and any(
                cls.value in row.missing_reasons
                for cls in (
                    ResultClassification.RATE_LIMITED,
                    ResultClassification.TIMEOUT,
                    ResultClassification.TLS_FAILURE,
                    ResultClassification.TRANSIENT_TRANSPORT_FAILURE,
                    ResultClassification.TRANSIENT_5XX,
                    ResultClassification.HTTP_FAILURE,
                    ResultClassification.INTERRUPTED_RESUMABLE,
                )
            )
        ):
            failure_days[(row.city, row.station)].add(row.date)
    del ledger_classifications
    clusters: list[dict[str, Any]] = []
    for (city, station), days in sorted(failure_days.items()):
        for start, end, length in _consecutive_runs(
            sorted(days), SYSTEMATIC_CLUSTER_CONSECUTIVE_DAYS
        ):
            clusters.append(
                {
                    "city": city,
                    "end": end,
                    "length_days": length,
                    "start": start,
                    "station": station,
                    "view": "consecutive_week_city_provider",
                }
            )
    cluster_ok = len(clusters) <= MAX_UNRESOLVED_SYSTEMATIC_CLUSTERS

    provenance_complete = hash_failures <= RAW_HASH_FAILURE_MAX
    pit = future <= FUTURE_LEAKAGE_MAX and retrospective <= RETROSPECTIVE_SUBSTITUTION_MAX
    topology = invalid_groups <= UNREVIEWED_INVALID_GROUP_MAX
    coverage_ok = overall_ok and city_ok and lead_ok and month_ok
    blocked: list[str] = []
    if not expected_t:
        blocked.append("empty_expected_universe")
    if future > FUTURE_LEAKAGE_MAX:
        blocked.append("future_leakage")
    if retrospective > RETROSPECTIVE_SUBSTITUTION_MAX:
        blocked.append("retrospective_substitution")
    if hash_failures > RAW_HASH_FAILURE_MAX:
        blocked.append("raw_provenance_hash_failures")
    if invalid_groups > UNREVIEWED_INVALID_GROUP_MAX:
        blocked.append("unreviewed_invalid_groups")
    if not overall_ok:
        blocked.append("overall_coverage")
    if not city_ok:
        blocked.append("city_coverage")
    if not lead_ok:
        blocked.append("lead_coverage")
    if not month_ok:
        blocked.append("month_coverage")
    if not settlement_ok:
        blocked.append("settlement_coverage")
    if not clob_ok:
        blocked.append("clob_coverage")
    if not cluster_ok:
        blocked.append("systematic_failure_cluster")

    ready = (
        provenance_complete
        and pit
        and topology
        and settlement_ok
        and clob_ok
        and coverage_ok
        and cluster_ok
        and not blocked
    )
    return DatasetAuditResult(
        phase35_dataset_ready=ready,
        provenance_complete=provenance_complete,
        point_in_time_integrity=pit,
        topology_integrity=topology,
        settlement_availability_acceptable=settlement_ok,
        descriptive_price_coverage_acceptable=clob_ok,
        coverage_policy_accepted=coverage_ok,
        no_unresolved_systematic_failure_cluster=cluster_ok,
        future_leakage_count=future,
        retrospective_substitution_count=retrospective,
        raw_provenance_hash_failures=hash_failures,
        invalid_event_group_count=invalid_groups,
        matrices=matrices,
        blocked_reasons=tuple(blocked),
        systematic_clusters=tuple(clusters),
    )


def point_in_time_flags(
    *,
    event_date: str,
    timezone_name: str,
    lead_hours: int,
    canonical_event_key: tuple[str, ...],
    forecasts: tuple[Any, ...] | list[Any],
    prices: tuple[PricePoint, ...] | list[PricePoint],
) -> tuple[bool, bool, tuple[str, ...]]:
    selected = select_checkpoint_inputs(
        event_date=event_date,
        timezone_name=timezone_name,
        lead_hours=lead_hours,
        canonical_event_key=canonical_event_key,
        forecasts=forecasts,
        prices=prices,
    )
    # V2 actual-future-leakage semantics: only a selected value later than
    # decision_ts counts. Post-decision-only absence remains in rejection reasons.
    future_forecast = bool(
        selected.forecast is not None and selected.forecast.available_at > selected.decision_ts
    )
    future_price = bool(
        selected.price is not None and selected.price.observed_at > selected.decision_ts
    )
    return future_forecast, future_price, selected.rejection_reasons


def build_dataset_audit_reports(
    audit: DatasetAuditResult,
    *,
    collection_not_executed: bool = True,
    v2_audit: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    measured_data: dict[str, Any] = {
        "PHASE35_DATASET_READY": audit.phase35_dataset_ready,
        "matrices": audit.as_dict()["matrices"],
        "systematic_clusters": list(audit.systematic_clusters),
    }
    model_output: dict[str, Any] = {
        "blocked_reasons": list(audit.blocked_reasons),
        "coverage_policy_accepted": audit.coverage_policy_accepted,
        "descriptive_price_coverage_acceptable": audit.descriptive_price_coverage_acceptable,
        "no_unresolved_systematic_failure_cluster": (
            audit.no_unresolved_systematic_failure_cluster
        ),
        "point_in_time_integrity": audit.point_in_time_integrity,
        "provenance_complete": audit.provenance_complete,
        "settlement_availability_acceptable": audit.settlement_availability_acceptable,
        "topology_integrity": audit.topology_integrity,
    }
    if v2_audit is not None:
        measured_data["PHASE35B_V2_AUDIT"] = dict(v2_audit)
        model_output["PHASE35B_V2_DATASET_READY"] = v2_audit.get(
            "PHASE35B_V2_DATASET_READY", "NOT_YET_ESTABLISHED"
        )
        model_output["POINT_IN_TIME_INTEGRITY_READY"] = v2_audit.get(
            "POINT_IN_TIME_INTEGRITY_READY"
        )
        model_output["FORECAST_CALIBRATION_DATA_READY"] = v2_audit.get(
            "FORECAST_CALIBRATION_DATA_READY"
        )
        model_output["FIXED_TIME_MARKET_ALPHA_DATA_READY"] = v2_audit.get(
            "FIXED_TIME_MARKET_ALPHA_DATA_READY"
        )
        model_output["EARLY_MARKET_ALPHA_DATA_READY"] = v2_audit.get(
            "EARLY_MARKET_ALPHA_DATA_READY"
        )
    machine = research_contract(
        measured_data=measured_data,
        model_output=model_output,
        assumptions={
            "city_coverage_min": CITY_COVERAGE_MIN,
            "clob_city_min": CLOB_CITY_MIN,
            "clob_overall_min": CLOB_OVERALL_MIN,
            "lead_coverage_min": LEAD_COVERAGE_MIN,
            "month_coverage_min": MONTH_COVERAGE_MIN,
            "overall_coverage_min": OVERALL_COVERAGE_MIN,
            "settlement_overall_min": SETTLEMENT_OVERALL_MIN,
            "settlement_scored_min": SETTLEMENT_SCORED_MIN,
            "systematic_cluster_consecutive_days": SYSTEMATIC_CLUSTER_CONSECUTIVE_DAYS,
            "systematic_cluster_max_unresolved": MAX_UNRESOLVED_SYSTEMATIC_CLUSTERS,
        },
        missing_data={"blocked_reasons": list(audit.blocked_reasons)},
        inferences={
            "PHASE35_DATASET_READY": audit.phase35_dataset_ready,
            "collection_not_executed": collection_not_executed,
        },
        limitations={
            "GAMMA_SURVIVORSHIP_LIMITATION": True,
            "HISTORICAL_CLOB_MODE": "descriptive_only",
            "HISTORICAL_UNIVERSE_COMPLETE": "not_proven",
        },
    )
    human = render_markdown(
        title="phase35_dataset_acceptance",
        measured=(
            f"PHASE35_DATASET_READY={audit.phase35_dataset_ready}",
            f"future_leakage_count={audit.future_leakage_count}",
            f"retrospective_substitution_count={audit.retrospective_substitution_count}",
            f"raw_provenance_hash_failures={audit.raw_provenance_hash_failures}",
        ),
        model_output=(
            f"coverage_policy_accepted={audit.coverage_policy_accepted}",
            f"blocked={','.join(audit.blocked_reasons) or 'none'}",
        ),
        assumptions=(
            f"overall>={OVERALL_COVERAGE_MIN}",
            f"city>={CITY_COVERAGE_MIN}",
            f"lead>={LEAD_COVERAGE_MIN}",
            f"month>={MONTH_COVERAGE_MIN}",
            f"systematic consecutive-week threshold={SYSTEMATIC_CLUSTER_CONSECUTIVE_DAYS}d; max unresolved={MAX_UNRESOLVED_SYSTEMATIC_CLUSTERS}",
        ),
        missing=tuple(audit.blocked_reasons) or ("none",),
        inferences=(
            "PHASE35_DATASET_READY is evaluated without collecting.",
            "Historical CLOB remains DESCRIPTIVE_ONLY.",
        ),
    )
    assert_text_has_no_machine_roots(human)
    return machine, human


@dataclass(frozen=True, slots=True)
class DatasetFreeze:
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def build_dataset_freeze(
    audit: DatasetAuditResult,
    *,
    collection_id: str,
    code_commit: str,
    manifest_sha256: str,
    raw_index_sha256: str,
    canonical_dataset_sha256: str,
    report_sha256: str,
    date_range: dict[str, str],
    event_count: int,
    snapshot_count: int,
    checkpoint_counts: dict[str, int],
    city_counts: dict[str, int],
    missingness_summary: dict[str, Any],
    quarantine_summary: dict[str, Any],
) -> DatasetFreeze | None:
    if not audit.phase35_dataset_ready:
        return None
    payload = {
        "CANONICAL_DATASET_SHA256": canonical_dataset_sha256,
        "CHECKPOINT_COUNTS": dict(checkpoint_counts),
        "CITY_COUNTS": dict(city_counts),
        "CODE_COMMIT": code_commit,
        "COLLECTION_ID": collection_id,
        "DATASET_ID": f"phase35-dataset-{collection_id}",
        "DATE_RANGE": dict(date_range),
        "EVENT_COUNT": event_count,
        "KNOWN_LIMITATIONS": {
            "GAMMA_SURVIVORSHIP_LIMITATION": True,
            "HISTORICAL_CLOB_MODE": "descriptive_only",
            "HISTORICAL_UNIVERSE_COMPLETE": "not_proven",
        },
        "MANIFEST_SHA256": manifest_sha256,
        "MISSINGNESS_SUMMARY": dict(missingness_summary),
        "QUARANTINE_SUMMARY": dict(quarantine_summary),
        "RAW_INDEX_SHA256": raw_index_sha256,
        "REPORT_SHA256": report_sha256,
        "SNAPSHOT_COUNT": snapshot_count,
    }
    return DatasetFreeze(payload=payload)
