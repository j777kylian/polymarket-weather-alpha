"""Phase 3.5b V2 offline dataset semantics and correction planning.

This module is additive and does not mutate or reinterpret persisted V1 artifacts.
Production wiring recomputes V2 truth from immutable persisted sources.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from weather_alpha.phase35.checkpoints import decision_timestamp
from weather_alpha.phase35.full_collection.audit import ExpectedCell
from weather_alpha.phase35.full_collection.clob_contract import (
    canonical_clob_identity,
    clob_window_timestamps,
    plan_clob_gets,
)
from weather_alpha.phase35.full_collection.ledger import (
    COMPLETE_REUSABLE,
    AppendOnlyLedger,
    LedgerRecord,
)
from weather_alpha.phase35.full_collection.policy import CLOB_FIDELITY_MINUTES
from weather_alpha.research.prices import PricePoint, select_price_at_or_before

LEFT_CENSOR_SECONDS = 3600
MARKET_RELATIVE_TARGET_HOURS: tuple[int, ...] = (0, 1, 3, 6, 12)
FIXED_CHECKPOINT_HOURS: tuple[int, ...] = (48, 24, 12, 6, 3, 1)


class PitClassification(StrEnum):
    ACTUAL_FUTURE_LEAKAGE = "ACTUAL_FUTURE_LEAKAGE"
    NO_PRE_DECISION_PRICE = "NO_PRE_DECISION_PRICE"
    PRICE_HISTORY_EMPTY = "PRICE_HISTORY_EMPTY"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    SCHEMA_FAILURE = "SCHEMA_FAILURE"
    UNRESOLVED_CORRECTION_REQUIRED = "UNRESOLVED_CORRECTION_REQUIRED"
    PIT_VALID = "PIT_VALID"


@dataclass(frozen=True, slots=True)
class MarketTarget:
    label: str
    analysis_target_ts: datetime
    selected_market_price: float | None
    selected_market_price_observed_at: datetime | None
    pit_valid: bool
    price_age_at_target_seconds: float | None
    eligible_within_boundary: bool


@dataclass(frozen=True, slots=True)
class T0Axis:
    request_start_ts: datetime
    first_observed_price_ts: datetime | None
    seconds_from_window_start: float | None
    event_time_minus_first_observed_price_seconds: float | None
    t0_left_censored: bool
    t0_uncensored: bool


@dataclass(frozen=True, slots=True)
class TrackEligibility:
    track_a_forecast_calibration: bool
    track_b_fixed_time_market_alpha: bool
    track_c_early_market_alpha_primary: bool
    track_c_left_censored_cohort: bool


@dataclass(frozen=True, slots=True)
class V2CountMatrix:
    EXPECTED_CELL_COUNT: int
    FORECAST_AVAILABLE_COUNT: int
    MARKET_PRICE_HISTORY_PRESENT_COUNT: int
    MARKET_OBSERVABLE_COUNT: int
    MARKET_UNOBSERVABLE_COUNT: int
    PIPELINE_SUCCESS_COUNT: int
    ANALYSIS_ELIGIBLE_COUNT: int
    ACTUAL_FUTURE_LEAKAGE_COUNT: int
    NO_PRE_DECISION_PRICE_COUNT: int
    PRICE_HISTORY_EMPTY_COUNT: int
    PROVIDER_FAILURE_COUNT: int
    SCHEMA_FAILURE_COUNT: int


@dataclass(frozen=True, slots=True)
class V2Readiness:
    PHASE35_V1_DATASET_READY: str
    PHASE35B_V2_PROTOCOL_PROPOSED: str
    PHASE35B_V2_IMPLEMENTED: str
    PHASE35B_V2_FROZEN: str
    POINT_IN_TIME_INTEGRITY_READY: str
    FORECAST_CALIBRATION_DATA_READY: str
    FIXED_TIME_MARKET_ALPHA_DATA_READY: str
    EARLY_MARKET_ALPHA_DATA_READY: str
    PHASE35B_V2_DATASET_READY: str


@dataclass(frozen=True, slots=True)
class CorrectionIdentityProvenance:
    identity: str
    event_family_id: str
    incorrect_old_identity: str | None
    incorrect_old_token: str | None
    correct_token: str
    start_ts: int
    end_ts: int
    fidelity: int
    ledger_evidence_collection_id: str | None


@dataclass(frozen=True, slots=True)
class CorrectionPlan:
    correction_recovery_required: bool
    correction_clob_identity_count: int
    correction_gamma_identity_count: int
    correction_ecmwf_identity_count: int
    cross_assigned_family_count: int
    token_ownership_violation_count: int
    unresolved_correction_clob_identities: tuple[str, ...]
    provenance: tuple[CorrectionIdentityProvenance, ...] = ()


def classify_market_pit(
    *,
    selected_price: PricePoint | None,
    decision_ts: datetime,
    price_history_present: bool,
    provider_failure: bool = False,
    schema_failure: bool = False,
) -> PitClassification:
    if provider_failure:
        return PitClassification.PROVIDER_FAILURE
    if schema_failure:
        return PitClassification.SCHEMA_FAILURE
    if selected_price is not None and selected_price.observed_at > decision_ts:
        return PitClassification.ACTUAL_FUTURE_LEAKAGE
    if selected_price is None and not price_history_present:
        return PitClassification.PRICE_HISTORY_EMPTY
    if selected_price is None:
        return PitClassification.NO_PRE_DECISION_PRICE
    return PitClassification.PIT_VALID


def derive_t0_axis(
    *,
    request_start_ts: datetime,
    event_ts: datetime,
    points: tuple[PricePoint, ...] | list[PricePoint],
) -> T0Axis:
    ordered = sorted(points, key=lambda row: row.observed_at)
    first = ordered[0].observed_at if ordered else None
    if first is None:
        return T0Axis(
            request_start_ts=request_start_ts,
            first_observed_price_ts=None,
            seconds_from_window_start=None,
            event_time_minus_first_observed_price_seconds=None,
            t0_left_censored=False,
            t0_uncensored=False,
        )
    delta = (first - request_start_ts).total_seconds()
    left = delta <= LEFT_CENSOR_SECONDS
    return T0Axis(
        request_start_ts=request_start_ts,
        first_observed_price_ts=first,
        seconds_from_window_start=delta,
        event_time_minus_first_observed_price_seconds=(event_ts - first).total_seconds(),
        t0_left_censored=left,
        t0_uncensored=not left,
    )


def market_relative_targets(
    *,
    t0_ts: datetime | None,
    points: tuple[PricePoint, ...] | list[PricePoint],
    valid_boundary_end_ts: datetime,
) -> tuple[MarketTarget, ...]:
    if t0_ts is None:
        return ()
    out: list[MarketTarget] = []
    for hours in MARKET_RELATIVE_TARGET_HOURS:
        target_ts = t0_ts + timedelta(hours=hours)
        selected = select_price_at_or_before(points, target_ts)
        eligible = target_ts <= valid_boundary_end_ts
        pit_valid = selected is not None and selected.observed_at <= target_ts
        age = None
        if selected is not None:
            age = (target_ts - selected.observed_at).total_seconds()
        out.append(
            MarketTarget(
                label=f"T0+{hours}h" if hours else "T0",
                analysis_target_ts=target_ts,
                selected_market_price=selected.price if selected is not None else None,
                selected_market_price_observed_at=(
                    selected.observed_at if selected is not None else None
                ),
                pit_valid=pit_valid,
                price_age_at_target_seconds=age,
                eligible_within_boundary=eligible,
            )
        )
    return tuple(out)


def derive_track_eligibility(
    *,
    forecast_pit_valid: bool,
    settlement_present: bool,
    market_pit_valid: bool,
    t0_uncensored: bool,
    within_boundary: bool,
) -> TrackEligibility:
    track_a = forecast_pit_valid and settlement_present
    track_b = track_a and market_pit_valid
    track_c_primary = track_a and market_pit_valid and within_boundary and t0_uncensored
    track_c_censored = track_a and market_pit_valid and within_boundary and (not t0_uncensored)
    return TrackEligibility(
        track_a_forecast_calibration=track_a,
        track_b_fixed_time_market_alpha=track_b,
        track_c_early_market_alpha_primary=track_c_primary,
        track_c_left_censored_cohort=track_c_censored,
    )


def summarize_v2_counts(rows: list[dict[str, Any]]) -> V2CountMatrix:
    expected = len(rows)
    return V2CountMatrix(
        EXPECTED_CELL_COUNT=expected,
        FORECAST_AVAILABLE_COUNT=sum(1 for row in rows if row.get("forecast_available")),
        MARKET_PRICE_HISTORY_PRESENT_COUNT=sum(
            1 for row in rows if row.get("market_price_history_present")
        ),
        MARKET_OBSERVABLE_COUNT=sum(1 for row in rows if row.get("market_observable")),
        MARKET_UNOBSERVABLE_COUNT=sum(1 for row in rows if not row.get("market_observable")),
        PIPELINE_SUCCESS_COUNT=sum(1 for row in rows if row.get("pipeline_success")),
        ANALYSIS_ELIGIBLE_COUNT=sum(1 for row in rows if row.get("analysis_eligible")),
        ACTUAL_FUTURE_LEAKAGE_COUNT=sum(
            1
            for row in rows
            if row.get("pit_classification") == PitClassification.ACTUAL_FUTURE_LEAKAGE.value
        ),
        NO_PRE_DECISION_PRICE_COUNT=sum(
            1
            for row in rows
            if row.get("pit_classification") == PitClassification.NO_PRE_DECISION_PRICE.value
        ),
        PRICE_HISTORY_EMPTY_COUNT=sum(
            1
            for row in rows
            if row.get("pit_classification") == PitClassification.PRICE_HISTORY_EMPTY.value
        ),
        PROVIDER_FAILURE_COUNT=sum(
            1
            for row in rows
            if row.get("pit_classification") == PitClassification.PROVIDER_FAILURE.value
        ),
        SCHEMA_FAILURE_COUNT=sum(
            1
            for row in rows
            if row.get("pit_classification") == PitClassification.SCHEMA_FAILURE.value
        ),
    )


def _token_owners(families: list[dict[str, Any]]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for family in families:
        family_id = str(family.get("event_family_id") or "")
        for token in family.get("yes_token_ids") or ():
            owners.setdefault(str(token), set()).add(family_id)
    return owners


def _canonical_owned_token(family: dict[str, Any], owners: dict[str, set[str]]) -> str | None:
    tokens = [str(token) for token in (family.get("yes_token_ids") or [])]
    owned = [token for token in tokens if len(owners.get(token) or set()) == 1]
    if not owned:
        return None
    return min(owned)


def _ledger_records_by_identity(
    ledger_path: Path | None,
) -> dict[str, LedgerRecord]:
    if ledger_path is None or not ledger_path.is_file():
        return {}
    out: dict[str, LedgerRecord] = {}
    for row in AppendOnlyLedger(ledger_path).records():
        # Last record wins; recovery ledgers append attempts chronologically.
        out[row.canonical_request_identity] = row
    return out


def _successful_ledger_identities(ledger_path: Path | None) -> set[str]:
    if ledger_path is None or not ledger_path.is_file():
        return set()
    ledger = AppendOnlyLedger(ledger_path)
    return {
        row.canonical_request_identity
        for row in ledger.records()
        if row.result_classification in COMPLETE_REUSABLE
    }


def _ledger_collection_id(ledger_path: Path | None) -> str | None:
    if ledger_path is None or not ledger_path.is_file():
        return None
    records = AppendOnlyLedger(ledger_path).records()
    if not records:
        return None
    return records[0].collection_id


def _request_params_for_identity(
    *,
    identity: str,
    ledger_by_identity: dict[str, LedgerRecord],
    parsed_by_identity: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Prefer persisted ledger request params; fall back to parsed only if absent."""

    ledger_row = ledger_by_identity.get(identity)
    if ledger_row is not None:
        params = dict(ledger_row.normalized_request_parameters or {})
        if params:
            return params
    parsed = parsed_by_identity.get(identity) or {}
    raw = parsed.get("params") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def derive_correction_plan(
    *,
    families: list[dict[str, Any]],
    clob_cell_map: dict[str, list[dict[str, Any]]],
    clob_parsed: list[dict[str, Any]],
    ledger_path: Path | None = None,
    successful_identities: set[str] | None = None,
) -> CorrectionPlan:
    """Derive CLOB-only correction identities from ownership + ledger evidence.

    The persisted first-recovery ledger is authoritative for originally requested
    CLOB identity/token/window and for already-successful collected identities.
    Missing canonical family-owned identities are derived from that ledger
    evidence (map/parsed alone are insufficient when they disagree or omit
    request params).
    """

    family_by_id = {str(row.get("event_family_id")): row for row in families}
    owners = _token_owners(families)
    ambiguous_tokens = {token for token, owned_by in owners.items() if len(owned_by) > 1}

    parsed_by_identity = {str(row.get("identity") or ""): row for row in clob_parsed}
    ledger_by_identity = _ledger_records_by_identity(ledger_path)
    mapped_identity_by_family: dict[str, str] = {}
    mapped_token_by_family: dict[str, str] = {}
    mapped_window_by_family: dict[str, tuple[int, int, int]] = {}
    for identity, cells in clob_cell_map.items():
        identity_key = str(identity)
        params = _request_params_for_identity(
            identity=identity_key,
            ledger_by_identity=ledger_by_identity,
            parsed_by_identity=parsed_by_identity,
        )
        params_market = str(params.get("market") or "")
        start_raw = params.get("startTs")
        end_raw = params.get("endTs")
        fidelity_raw = params.get("fidelity")
        for cell in cells:
            family_id = str(cell.get("event_family_id") or "")
            if not family_id:
                continue
            mapped_identity_by_family[family_id] = identity_key
            if params_market:
                mapped_token_by_family[family_id] = params_market
            if start_raw is not None and end_raw is not None:
                fidelity = (
                    int(fidelity_raw) if fidelity_raw is not None else int(CLOB_FIDELITY_MINUTES)
                )
                mapped_window_by_family[family_id] = (
                    int(start_raw),
                    int(end_raw),
                    fidelity,
                )

    if successful_identities is not None:
        resolved_identities = set(successful_identities)
    elif ledger_path is not None:
        resolved_identities = _successful_ledger_identities(ledger_path)
    else:
        resolved_identities = set(parsed_by_identity)

    ledger_collection_id = _ledger_collection_id(ledger_path)
    unresolved: list[str] = []
    provenance_rows: list[CorrectionIdentityProvenance] = []
    cross_assigned = 0
    violations = 0
    affected_families: set[str] = set()

    for family_id, family in family_by_id.items():
        tokens = [str(token) for token in family.get("yes_token_ids") or ()]
        if not tokens:
            continue
        owned_unambiguous = [token for token in tokens if token not in ambiguous_tokens]
        if not owned_unambiguous:
            violations += 1
            affected_families.add(family_id)
            continue
        selected = min(owned_unambiguous)
        mapped_token = mapped_token_by_family.get(family_id)
        if mapped_token is not None and mapped_token != selected:
            cross_assigned += 1
            affected_families.add(family_id)
        if mapped_token is not None and mapped_token not in tokens:
            violations += 1
            affected_families.add(family_id)

    for family_id in sorted(affected_families):
        family = family_by_id[family_id]
        selected_token = _canonical_owned_token(family, owners)
        if selected_token is None:
            continue
        window = mapped_window_by_family.get(family_id)
        if window is not None:
            start_ts, end_ts, fidelity = window
        else:
            timezone_name = str(family.get("timezone_name") or "UTC")
            day = str(family.get("date"))
            start_ts, end_ts = clob_window_timestamps(day, timezone_name)
            fidelity = int(CLOB_FIDELITY_MINUTES)
        identity = canonical_clob_identity(
            market=selected_token,
            start_ts=start_ts,
            end_ts=end_ts,
            fidelity=fidelity,
        )
        if identity in resolved_identities:
            continue
        unresolved.append(identity)
        old_identity = mapped_identity_by_family.get(family_id)
        provenance_rows.append(
            CorrectionIdentityProvenance(
                identity=identity,
                event_family_id=family_id,
                incorrect_old_identity=old_identity,
                incorrect_old_token=mapped_token_by_family.get(family_id),
                correct_token=selected_token,
                start_ts=start_ts,
                end_ts=end_ts,
                fidelity=fidelity,
                ledger_evidence_collection_id=ledger_collection_id,
            )
        )

    unresolved_sorted = tuple(sorted(dict.fromkeys(unresolved)))
    provenance_sorted = tuple(
        sorted(provenance_rows, key=lambda row: (row.identity, row.event_family_id))
    )
    return CorrectionPlan(
        correction_recovery_required=bool(unresolved_sorted),
        correction_clob_identity_count=len(unresolved_sorted),
        correction_gamma_identity_count=0,
        correction_ecmwf_identity_count=0,
        cross_assigned_family_count=cross_assigned,
        token_ownership_violation_count=violations,
        unresolved_correction_clob_identities=unresolved_sorted,
        provenance=provenance_sorted,
    )


def corrected_planner_ownership_counts(
    *,
    expected: tuple[ExpectedCell, ...] | list[ExpectedCell],
    families: list[dict[str, Any]],
) -> tuple[int, int]:
    """Recompute planning ownership under the production planner (fail-closed)."""

    owners = _token_owners(families)
    plans, mapping = plan_clob_gets(expected, families)
    planned_token_by_identity = {
        plan.identity: str(plan.params.get("market") or "") for plan in plans
    }
    cross_assigned = 0
    violations = 0
    seen_families: set[str] = set()
    for identity, cells in mapping.items():
        token = planned_token_by_identity.get(identity, "")
        for cell in cells:
            family_id = str(cell.get("event_family_id") or "")
            if not family_id or family_id in seen_families:
                continue
            seen_families.add(family_id)
            family = next(
                (row for row in families if str(row.get("event_family_id")) == family_id),
                None,
            )
            if family is None:
                continue
            tokens = {str(item) for item in (family.get("yes_token_ids") or [])}
            if not token or token not in tokens:
                violations += 1
            if len(owners.get(token) or set()) > 1:
                cross_assigned += 1
    return cross_assigned, violations


def v2_readiness_state(
    *,
    v1_ready: str = "NO",
    v2_implemented: bool,
    correction_recovery_executed: bool,
    final_v2_audit_passed: bool,
    frozen: bool,
    unresolved_correction_count: int = 0,
    track_a_support: bool = False,
) -> V2Readiness:
    # Fail closed: unresolved corrections can never yield DATASET_READY=YES.
    # DATASET_READY is pre-freeze eligibility; FROZEN is an independent post-freeze fact.
    # Affirmative READY also requires track_a_support (implemented + recovery + audit + unresolved==0).
    if unresolved_correction_count > 0:
        dataset_ready = "NOT_YET_ESTABLISHED"
    elif (
        v2_implemented
        and correction_recovery_executed
        and final_v2_audit_passed
        and track_a_support
    ):
        dataset_ready = "YES"
    else:
        dataset_ready = "NOT_YET_ESTABLISHED"

    if not v2_implemented:
        pit_ready = "NO"
        forecast_ready = "NO"
        fixed_ready = "NO"
        early_ready = "NO"
    elif unresolved_correction_count > 0 or not correction_recovery_executed:
        pit_ready = "YES"
        forecast_ready = "YES" if track_a_support else "NO"
        fixed_ready = "BLOCKED_PENDING_CORRECTION"
        early_ready = "BLOCKED_PENDING_CORRECTION"
    else:
        pit_ready = "YES"
        forecast_ready = "YES" if track_a_support else "NO"
        fixed_ready = dataset_ready
        early_ready = dataset_ready

    return V2Readiness(
        PHASE35_V1_DATASET_READY=v1_ready,
        PHASE35B_V2_PROTOCOL_PROPOSED="YES",
        PHASE35B_V2_IMPLEMENTED="YES" if v2_implemented else "NO",
        PHASE35B_V2_FROZEN="YES" if frozen else "NO",
        POINT_IN_TIME_INTEGRITY_READY=pit_ready,
        FORECAST_CALIBRATION_DATA_READY=forecast_ready,
        FIXED_TIME_MARKET_ALPHA_DATA_READY=fixed_ready,
        EARLY_MARKET_ALPHA_DATA_READY=early_ready,
        PHASE35B_V2_DATASET_READY=dataset_ready,
    )


def _parse_price_points(row: dict[str, Any]) -> list[PricePoint]:
    points: list[PricePoint] = []
    for item in row.get("points") or []:
        if not isinstance(item, dict) or item.get("observed_at") is None:
            continue
        observed = datetime.fromisoformat(str(item["observed_at"]))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        price_raw = item.get("price")
        price = None if price_raw is None else float(price_raw)
        points.append(PricePoint(observed_at=observed.astimezone(UTC), price=price))
    return points


def _event_midnight_utc(family: dict[str, Any]) -> datetime:
    year, month, day = (int(part) for part in str(family["date"]).split("-"))
    local = datetime(year, month, day, 0, 0, tzinfo=ZoneInfo(str(family["timezone_name"])))
    return local.astimezone(UTC)


def offline_v2_corpus_audit(collection_namespace: Path) -> dict[str, Any]:
    """Production V2 offline audit: recompute from immutable persisted sources."""

    families = _load_json_list(collection_namespace / "events" / "accepted.json")
    expected_raw = _load_json_list(collection_namespace / "expected_cells.json")
    clob_map = _load_json_object(collection_namespace / "plans" / "clob_cell_map.json")
    clob_parsed = _load_json_list(collection_namespace / "parsed" / "clob.json")
    ledger_path = collection_namespace / "ledger.jsonl"

    family_by_id = {str(row.get("event_family_id")): row for row in families}
    expected_cells = [ExpectedCell.from_dict(row) for row in expected_raw]
    if not expected_cells:
        # Synthetic / minimal namespaces used by unit tests.
        expected_cells = [
            ExpectedCell(
                date=str(family.get("date") or "1970-01-01"),
                city=str(family.get("city") or "unknown"),
                station=str(family.get("station") or "UNK"),
                checkpoint=checkpoint,
                event_family_id=str(family.get("event_family_id") or ""),
                month=str(family.get("date") or "1970-01-01")[:7],
                ecmwf_run_cycle=None,
            )
            for family in families
            for checkpoint in FIXED_CHECKPOINT_HOURS
            if family.get("event_family_id")
        ]

    clob_cell_map = {key: value for key, value in clob_map.items() if isinstance(value, list)}
    correction = derive_correction_plan(
        families=families,
        clob_cell_map=clob_cell_map,
        clob_parsed=clob_parsed,
        ledger_path=ledger_path if ledger_path.is_file() else None,
    )
    corrected_cross, corrected_violations = corrected_planner_ownership_counts(
        expected=expected_cells,
        families=families,
    )

    unresolved_families = {row.event_family_id for row in correction.provenance}

    parsed_by_identity = {str(row.get("identity") or ""): row for row in clob_parsed}
    cell_to_identity: dict[tuple[str, int], str] = {}
    identity_to_families: dict[str, set[str]] = {}
    for identity, cells in clob_cell_map.items():
        for cell in cells:
            family_id = str(cell.get("event_family_id") or "")
            checkpoint = int(cell.get("checkpoint") or 0)
            if family_id and checkpoint:
                cell_to_identity[(family_id, checkpoint)] = str(identity)
            if family_id:
                identity_to_families.setdefault(str(identity), set()).add(family_id)

    price_points_by_identity = {
        identity: _parse_price_points(row) for identity, row in parsed_by_identity.items()
    }
    history_present_by_identity = {
        identity: bool(points) for identity, points in price_points_by_identity.items()
    }

    t0_by_identity: dict[str, T0Axis] = {}
    market_age_by_identity: dict[str, tuple[MarketTarget, ...]] = {}
    for parsed_identity, row in parsed_by_identity.items():
        points = price_points_by_identity.get(parsed_identity) or []
        if not points:
            continue
        params = row.get("params") or {}
        start_ts = int(params.get("startTs") or 0)
        end_ts = int(params.get("endTs") or 0)
        request_start = datetime.fromtimestamp(start_ts, tz=UTC)
        request_end = datetime.fromtimestamp(end_ts, tz=UTC)
        mapped_family_ids = sorted(identity_to_families.get(parsed_identity) or ())
        family = None
        for mapped_family_id in mapped_family_ids:
            candidate = family_by_id.get(mapped_family_id)
            if candidate is None:
                continue
            token = str(params.get("market") or "")
            owned = {str(item) for item in (candidate.get("yes_token_ids") or [])}
            if token and token in owned and mapped_family_id not in unresolved_families:
                family = candidate
                break
        if family is None and mapped_family_ids:
            family = family_by_id.get(mapped_family_ids[0])
        event_ts = _event_midnight_utc(family) if family is not None else request_end
        axis = derive_t0_axis(request_start_ts=request_start, event_ts=event_ts, points=points)
        t0_by_identity[parsed_identity] = axis
        market_age_by_identity[parsed_identity] = market_relative_targets(
            t0_ts=axis.first_observed_price_ts,
            points=points,
            valid_boundary_end_ts=request_end,
        )

    pit_counts = {item.value: 0 for item in PitClassification}
    empty_families: set[str] = set()
    empty_cells = 0
    unresolved_cells = 0
    track_a = 0
    track_b = 0
    track_c_primary = 0
    track_c_censored = 0
    market_history_present_cells = 0
    market_observable_cells = 0
    market_unobservable_cells = 0
    checkpoint_market: dict[int, dict[str, int]] = {
        lead: {"observable": 0, "unobservable": 0, "track_b": 0} for lead in FIXED_CHECKPOINT_HOURS
    }
    market_age_support: dict[str, dict[str, int]] = {
        (f"T0+{hours}h" if hours else "T0"): {
            "eligible_denominator": 0,
            "pit_valid_count": 0,
            "left_censored_count": 0,
            "uncensored_count": 0,
        }
        for hours in MARKET_RELATIVE_TARGET_HOURS
    }

    for cell in expected_cells:
        family = family_by_id.get(cell.event_family_id)
        if family is None:
            continue
        excluded = cell.event_family_id in unresolved_families
        settlement_present = bool(family.get("has_settlement"))
        forecast_pit_valid = True

        if excluded:
            unresolved_cells += 1
            pit_counts[PitClassification.UNRESOLVED_CORRECTION_REQUIRED.value] += 1
            # Forecast-only Track A may still count; never use wrong market prices.
            tracks = derive_track_eligibility(
                forecast_pit_valid=forecast_pit_valid,
                settlement_present=settlement_present,
                market_pit_valid=False,
                t0_uncensored=False,
                within_boundary=False,
            )
            if tracks.track_a_forecast_calibration:
                track_a += 1
            continue

        timezone_name = str(family.get("timezone_name") or "UTC")
        decision = decision_timestamp(cell.date, timezone_name, cell.checkpoint)
        cell_identity = cell_to_identity.get((cell.event_family_id, cell.checkpoint))
        points = price_points_by_identity.get(cell_identity or "") or []
        history_present = history_present_by_identity.get(cell_identity or "", False)
        selected = select_price_at_or_before(points, decision)
        classification = classify_market_pit(
            selected_price=selected,
            decision_ts=decision,
            price_history_present=history_present,
        )
        pit_counts[classification.value] += 1
        if history_present:
            market_history_present_cells += 1
        if classification is PitClassification.PRICE_HISTORY_EMPTY:
            empty_cells += 1
            empty_families.add(cell.event_family_id)
        market_observable = history_present
        if market_observable:
            market_observable_cells += 1
        else:
            market_unobservable_cells += 1

        market_pit_valid = classification is PitClassification.PIT_VALID
        cell_axis = t0_by_identity.get(cell_identity or "")
        targets = market_age_by_identity.get(cell_identity or "") or ()
        # Primary Track-C boundary: T0 itself must fall within the collection window.
        within_boundary = bool(targets) and any(
            target.label == "T0" and target.eligible_within_boundary for target in targets
        )
        tracks = derive_track_eligibility(
            forecast_pit_valid=forecast_pit_valid,
            settlement_present=settlement_present,
            market_pit_valid=market_pit_valid,
            t0_uncensored=bool(cell_axis.t0_uncensored) if cell_axis is not None else False,
            within_boundary=within_boundary if cell_axis is not None else False,
        )
        if tracks.track_a_forecast_calibration:
            track_a += 1
        if tracks.track_b_fixed_time_market_alpha:
            track_b += 1
        if tracks.track_c_early_market_alpha_primary:
            track_c_primary += 1
        if tracks.track_c_left_censored_cohort:
            track_c_censored += 1

        if cell.checkpoint in checkpoint_market:
            bucket = checkpoint_market[cell.checkpoint]
            if market_observable:
                bucket["observable"] += 1
            else:
                bucket["unobservable"] += 1
            if tracks.track_b_fixed_time_market_alpha:
                bucket["track_b"] += 1

        if cell_axis is not None and market_pit_valid and settlement_present:
            for target in targets:
                support = market_age_support[target.label]
                if target.eligible_within_boundary:
                    support["eligible_denominator"] += 1
                    if target.pit_valid:
                        support["pit_valid_count"] += 1
                    if cell_axis.t0_left_censored:
                        support["left_censored_count"] += 1
                    if cell_axis.t0_uncensored:
                        support["uncensored_count"] += 1

    # T0 token geometry excludes identities that only serve unresolved families
    # or whose requested token is not owned by any resolved mapped family.
    t0_left = 0
    t0_uncensored = 0
    for identity, axis in t0_by_identity.items():
        params = (parsed_by_identity.get(identity) or {}).get("params") or {}
        token = str(params.get("market") or "")
        owns_resolved = False
        for family_id in identity_to_families.get(identity) or ():
            if family_id in unresolved_families:
                continue
            family = family_by_id.get(family_id)
            if family is None:
                continue
            owned = {str(item) for item in (family.get("yes_token_ids") or [])}
            if token and token in owned:
                owns_resolved = True
                break
        if not owns_resolved:
            continue
        if axis.t0_left_censored:
            t0_left += 1
        if axis.t0_uncensored:
            t0_uncensored += 1

    readiness = v2_readiness_state(
        v1_ready="NO",
        v2_implemented=True,
        correction_recovery_executed=False,
        final_v2_audit_passed=False,
        frozen=False,
        unresolved_correction_count=correction.correction_clob_identity_count,
        track_a_support=track_a > 0,
    )

    return {
        "EXPECTED_CELL_COUNT": len(expected_cells),
        "FORECAST_AVAILABLE_COUNT": track_a,
        "MARKET_PRICE_HISTORY_PRESENT_COUNT": market_history_present_cells,
        "MARKET_OBSERVABLE_COUNT": market_observable_cells,
        "MARKET_UNOBSERVABLE_COUNT": market_unobservable_cells,
        "PIPELINE_SUCCESS_COUNT": len(parsed_by_identity),
        "ANALYSIS_ELIGIBLE_COUNT": track_b,
        "ACTUAL_SELECTED_FUTURE_PRICE_COUNT": pit_counts[
            PitClassification.ACTUAL_FUTURE_LEAKAGE.value
        ],
        "ACTUAL_FUTURE_LEAKAGE_COUNT": pit_counts[PitClassification.ACTUAL_FUTURE_LEAKAGE.value],
        "NO_PRE_DECISION_PRICE_CELL_COUNT": pit_counts[
            PitClassification.NO_PRE_DECISION_PRICE.value
        ],
        "NO_PRE_DECISION_PRICE_COUNT": pit_counts[PitClassification.NO_PRE_DECISION_PRICE.value],
        "PRICE_HISTORY_EMPTY_FAMILY_COUNT": len(empty_families),
        "PRICE_HISTORY_EMPTY_CELL_COUNT": empty_cells,
        "PRICE_HISTORY_EMPTY_COUNT": empty_cells,
        "PROVIDER_FAILURE_COUNT": pit_counts[PitClassification.PROVIDER_FAILURE.value],
        "SCHEMA_FAILURE_COUNT": pit_counts[PitClassification.SCHEMA_FAILURE.value],
        "UNRESOLVED_CORRECTION_REQUIRED_CELL_COUNT": unresolved_cells,
        "CROSS_ASSIGNED_FAMILY_COUNT": correction.cross_assigned_family_count,
        "TOKEN_OWNERSHIP_VIOLATION_COUNT": correction.token_ownership_violation_count,
        "CROSS_ASSIGNED_FAMILY_COUNT_CORRECTED_PLANNER": corrected_cross,
        "TOKEN_OWNERSHIP_VIOLATION_COUNT_CORRECTED_PLANNER": corrected_violations,
        "UNRESOLVED_CORRECTION_CLOB_IDENTITIES": list(
            correction.unresolved_correction_clob_identities
        ),
        "UNRESOLVED_CORRECTION_CLOB_IDENTITY_COUNT": correction.correction_clob_identity_count,
        "UNRESOLVED_CORRECTION_CLOB_IDENTITIES_COUNT": correction.correction_clob_identity_count,
        "CORRECTION_GAMMA_IDENTITY_COUNT": correction.correction_gamma_identity_count,
        "CORRECTION_ECMWF_IDENTITY_COUNT": correction.correction_ecmwf_identity_count,
        "CORRECTION_PROVENANCE": [
            {
                "identity": row.identity,
                "event_family_id": row.event_family_id,
                "incorrect_old_identity": row.incorrect_old_identity,
                "incorrect_old_token": row.incorrect_old_token,
                "correct_token": row.correct_token,
                "start_ts": row.start_ts,
                "end_ts": row.end_ts,
                "fidelity": row.fidelity,
                "ledger_evidence_collection_id": row.ledger_evidence_collection_id,
            }
            for row in correction.provenance
        ],
        "T0_LEFT_CENSORED_TOKEN_COUNT": t0_left,
        "T0_UNCENSORED_TOKEN_COUNT": t0_uncensored,
        "TRACK_A_ELIGIBLE_COUNT": track_a,
        "TRACK_B_ELIGIBLE_COUNT": track_b,
        "TRACK_C_PRIMARY_ELIGIBLE_COUNT": track_c_primary,
        "TRACK_C_LEFT_CENSORED_ELIGIBLE_COUNT": track_c_censored,
        "48H_MARKET_OBSERVABLE_COUNT": checkpoint_market[48]["observable"],
        "48H_MARKET_UNOBSERVABLE_COUNT": checkpoint_market[48]["unobservable"],
        "CHECKPOINT_MARKET_SUPPORT": {
            str(lead): dict(values) for lead, values in checkpoint_market.items()
        },
        "MARKET_AGE_TARGET_SUPPORT": market_age_support,
        "PHASE35_V1_DATASET_READY": readiness.PHASE35_V1_DATASET_READY,
        "PHASE35B_V2_PROTOCOL_PROPOSED": readiness.PHASE35B_V2_PROTOCOL_PROPOSED,
        "PHASE35B_V2_IMPLEMENTED": readiness.PHASE35B_V2_IMPLEMENTED,
        "PHASE35B_V2_FROZEN": readiness.PHASE35B_V2_FROZEN,
        "POINT_IN_TIME_INTEGRITY_READY": readiness.POINT_IN_TIME_INTEGRITY_READY,
        "FORECAST_CALIBRATION_DATA_READY": readiness.FORECAST_CALIBRATION_DATA_READY,
        "FIXED_TIME_MARKET_ALPHA_DATA_READY": readiness.FIXED_TIME_MARKET_ALPHA_DATA_READY,
        "EARLY_MARKET_ALPHA_DATA_READY": readiness.EARLY_MARKET_ALPHA_DATA_READY,
        "PHASE35B_V2_DATASET_READY": readiness.PHASE35B_V2_DATASET_READY,
    }


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}
