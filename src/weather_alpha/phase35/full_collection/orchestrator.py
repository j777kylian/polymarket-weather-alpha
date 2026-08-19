"""Offline full historical collection orchestrator. Fake GET transports only in tests.

Does not create a collection manifest or authorization receipt. Live provider
network is derived only after a persisted authorization receipt binds the
immutable manifest digest, collection id, code commit, and request policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from weather_alpha.collectors.polymarket.collector import _markets_from_search
from weather_alpha.collectors.polymarket.parser import (
    ParsedGammaMarket,
    is_temperature_market_text,
    parse_gamma_market,
)
from weather_alpha.config.stations import Station
from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.models.timeutil import utc_now
from weather_alpha.phase35.checkpoints import (
    ForecastCandidate,
    decision_timestamp,
    select_forecast_at_or_before,
)
from weather_alpha.phase35.full_collection.audit import DatasetObservation, ExpectedCell
from weather_alpha.phase35.full_collection.budget import (
    BudgetEnforcement,
    DiskProbe,
    StaticDiskProbe,
    enforce_request_budget,
    estimate_full_collection_budget,
)
from weather_alpha.phase35.full_collection.clob_contract import plan_clob_gets
from weather_alpha.phase35.full_collection.executor import (
    AttemptOutcome,
    BoundedGetExecutor,
    CollectionCapExceeded,
    CollectionPreflightBlocked,
    PlannedGet,
)
from weather_alpha.phase35.full_collection.ledger import (
    AppendOnlyLedger,
    RawProvenanceHashFailure,
    ResultClassification,
)
from weather_alpha.phase35.full_collection.manifest import (
    AuthorizedManifest,
    load_authorized_manifest,
)
from weather_alpha.phase35.full_collection.policy import (
    CHECKPOINTS,
    ECMWF_ENDPOINT,
    FORECAST_MODEL,
    FORECAST_PROVIDER,
    GAMMA_ENDPOINT,
    MARKET_PROVIDER,
    PRICE_SELECTION_RULE,
    STORAGE_HARD_CAP_BYTES,
    STORAGE_PREFLIGHT_MIN_BYTES,
)
from weather_alpha.phase35.full_collection.provenance import (
    assert_text_has_no_machine_roots,
    atomic_write_json,
    probe_raw,
)
from weather_alpha.phase35.full_collection.retry import attempts_exhausted, is_retryable
from weather_alpha.phase35.full_collection.schedule import (
    catalog_stations,
    city_date_pairs,
    gamma_search_params,
    stations_for_city,
)
from weather_alpha.research.event_coverage import evaluate_single_run_event_coverage
from weather_alpha.research.event_groups import accept_event_groups
from weather_alpha.research.prices import (
    PricePoint,
    parse_price_history_points,
    select_price_at_or_before,
)
from weather_alpha.research.provider_schema import (
    ProviderSchemaError,
    classify_payload_semantic_class,
    validate_gamma_search_payload,
    validate_prices_history_payload,
    validate_single_run_payload,
)
from weather_alpha.research.settlement import parse_settlement_label
from weather_alpha.research.single_run import (
    SINGLE_RUN_HOURLY_VARIABLES,
    SINGLE_RUN_MODEL,
    availability_lag,
    choose_ecmwf_run,
    parse_single_run_forecast,
)
from weather_alpha.research.stations import resolve_research_station
from weather_alpha.research.types import (
    QuarantineRecord,
    ResearchSnapshot,
    event_group_key,
)

NO_VALID_FORECAST_BEFORE_DECISION = "NO_VALID_FORECAST_BEFORE_DECISION"
PRICE_HISTORY_EMPTY = "PRICE_HISTORY_EMPTY"
NO_PRE_DECISION_PRICE = "NO_PRE_DECISION_PRICE"


class CollectionStage(StrEnum):
    MANIFEST_READY = "MANIFEST_READY"
    GAMMA_DISCOVERY = "GAMMA_DISCOVERY"
    EVENT_ASSEMBLY = "EVENT_ASSEMBLY"
    ECMWF_PLANNING = "ECMWF_PLANNING"
    ECMWF_COLLECTION = "ECMWF_COLLECTION"
    CLOB_PLANNING = "CLOB_PLANNING"
    CLOB_COLLECTION = "CLOB_COLLECTION"
    CORPUS_ASSEMBLY = "CORPUS_ASSEMBLY"
    COMPLETE = "COMPLETE"
    INTERRUPTED_RESUMABLE = "INTERRUPTED_RESUMABLE"
    FAILED_INTEGRITY = "FAILED_INTEGRITY"


@dataclass(frozen=True, slots=True)
class CollectionRunResult:
    collection_id: str
    stage: CollectionStage
    collection_status: str
    collection_started: bool
    skipped_replay: bool
    accepted_family_count: int
    expected_cell_count: int
    interrupt_reason: str | None
    ledger: AppendOnlyLedger
    manifest_path: Path

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "accepted_family_count": self.accepted_family_count,
            "collection_id": self.collection_id,
            "collection_started": self.collection_started,
            "collection_status": self.collection_status,
            "expected_cell_count": self.expected_cell_count,
            "interrupt_reason": self.interrupt_reason,
            "skipped_replay": self.skipped_replay,
            "stage": self.stage.value,
        }
        assert_text_has_no_machine_roots(json.dumps(payload, sort_keys=True))
        return payload


class FullHistoricalCollectionService:
    """Execute one authorized, immutable, GET-only historical collection."""

    def __init__(
        self,
        *,
        manifest_path: Path,
        authorization_path: Path,
        collection_root: Path,
        http: ReadOnlyHttpClient,
        expected_code_commit: str,
        execution_pairs: tuple[tuple[str, str], ...] | None = None,
        enforcement: BudgetEnforcement | None = None,
        disk: DiskProbe | None = None,
        sleeper: Any = None,
        clock: Any = None,
        global_attempt_cap: int | None = None,
        force_post_decision_inputs: bool = False,
    ) -> None:
        self.manifest_path = manifest_path
        self.authorization_path = authorization_path
        self._collection_root = collection_root
        self._http = http
        self._expected_code_commit = expected_code_commit
        self._execution_pairs = execution_pairs
        self._enforcement_override = enforcement
        self._disk = disk or StaticDiskProbe(
            free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0
        )
        self._sleeper = sleeper or (lambda _seconds: None)
        self._clock = clock or utc_now
        self._global_attempt_cap = global_attempt_cap
        self._force_post_decision_inputs = force_post_decision_inputs
        self._new_gets = 0
        self._interrupt_reason: str | None = None
        self._stage = CollectionStage.MANIFEST_READY

    def run(self) -> CollectionRunResult:
        authorized = load_authorized_manifest(
            self.manifest_path,
            authorization_path=self.authorization_path,
            expected_code_commit=self._expected_code_commit,
        )
        namespace = self._collection_root / authorized.collection_id
        namespace.mkdir(parents=True, exist_ok=True)
        ledger = AppendOnlyLedger(namespace / "ledger.jsonl")
        base_enforcement = self._enforcement_override or enforce_request_budget(
            estimate_full_collection_budget(),
            disk=self._disk,
            storage_root=namespace,
        )
        enforcement = _overlay_network_authorization(
            base_enforcement,
            network_authorized=authorized.network_authorized,
        )
        if not enforcement.allowed:
            raise CollectionPreflightBlocked(enforcement.status)
        self._stage = CollectionStage.MANIFEST_READY
        _write_progress(namespace, authorized, self._stage, clock=self._clock())
        executor = BoundedGetExecutor(
            collection_id=authorized.collection_id,
            http=self._http,
            ledger=ledger,
            raw_root=namespace,
            enforcement=enforcement,
            disk=self._disk,
            global_attempt_cap=(
                self._global_attempt_cap
                if self._global_attempt_cap is not None
                else enforcement.estimate.global_max_attempts
            ),
            storage_hard_cap_bytes=STORAGE_HARD_CAP_BYTES,
            sleeper=self._sleeper,
            clock=self._clock,
        )
        try:
            self._verify_existing_hashes(namespace, ledger)
            pairs = self._execution_pairs or city_date_pairs()
            if not enforcement.network_authorized:
                raise CollectionPreflightBlocked(
                    enforcement.full_collection_start_allowed or enforcement.status
                )
            self._stage = CollectionStage.GAMMA_DISCOVERY
            _write_progress(namespace, authorized, self._stage, clock=self._clock())
            gamma_rows = self._discover_gamma(executor, namespace, pairs)
            _persist_json(namespace / "discovery" / "gamma_summaries.json", gamma_rows)
            self._stage = CollectionStage.EVENT_ASSEMBLY
            _write_progress(namespace, authorized, self._stage, clock=self._clock())
            families, quarantined = self._assemble_events(namespace, gamma_rows)
            _persist_json(namespace / "events" / "accepted.json", families)
            _persist_json(namespace / "events" / "quarantined.json", quarantined)
            expected = _expected_cells(families)
            _persist_json(namespace / "expected_cells.json", [row.as_dict() for row in expected])
            self._stage = CollectionStage.ECMWF_PLANNING
            _write_progress(namespace, authorized, self._stage, clock=self._clock())
            ecmwf_plans, ecmwf_map, expected = self._plan_ecmwf(expected, families)
            _persist_json(
                namespace / "plans" / "ecmwf.json",
                [plan_to_dict(row) for row in ecmwf_plans],
            )
            _persist_json(namespace / "plans" / "ecmwf_cell_map.json", ecmwf_map)
            _persist_json(namespace / "expected_cells.json", [row.as_dict() for row in expected])
            self._stage = CollectionStage.ECMWF_COLLECTION
            _write_progress(namespace, authorized, self._stage, clock=self._clock())
            ecmwf_parsed = self._collect_gets(
                executor, ecmwf_plans, kind="ecmwf", namespace=namespace
            )
            _persist_json(namespace / "parsed" / "ecmwf.json", ecmwf_parsed)
            self._stage = CollectionStage.CLOB_PLANNING
            _write_progress(namespace, authorized, self._stage, clock=self._clock())
            clob_plans, clob_map = self._plan_clob(expected, families)
            _persist_json(
                namespace / "plans" / "clob.json", [plan_to_dict(row) for row in clob_plans]
            )
            _persist_json(namespace / "plans" / "clob_cell_map.json", clob_map)
            self._stage = CollectionStage.CLOB_COLLECTION
            _write_progress(namespace, authorized, self._stage, clock=self._clock())
            clob_parsed = self._collect_gets(executor, clob_plans, kind="clob", namespace=namespace)
            _persist_json(namespace / "parsed" / "clob.json", clob_parsed)
            self._stage = CollectionStage.CORPUS_ASSEMBLY
            _write_progress(namespace, authorized, self._stage, clock=self._clock())
            observations = assemble_dataset_observations(
                expected=expected,
                families=families,
                ecmwf_parsed=ecmwf_parsed,
                ecmwf_map=ecmwf_map,
                clob_parsed=clob_parsed,
                clob_map=clob_map,
                ecmwf_namespace=namespace,
                clob_namespace=namespace,
                ecmwf_ledger=ledger,
                clob_ledger=ledger,
            )
            _persist_json(
                namespace / "selections" / "pit.json",
                [row.as_dict() for row in observations],
            )
            _persist_json(namespace / "observations.json", [row.as_dict() for row in observations])
            self._stage = CollectionStage.COMPLETE
            _write_progress(namespace, authorized, self._stage, clock=self._clock(), terminal=True)
            return CollectionRunResult(
                collection_id=authorized.collection_id,
                stage=self._stage,
                collection_status=self._stage.value,
                collection_started=True,
                skipped_replay=self._new_gets == 0 and bool(ledger.records()),
                accepted_family_count=len(families),
                expected_cell_count=len(expected),
                interrupt_reason=None,
                ledger=ledger,
                manifest_path=self.manifest_path,
            )
        except CollectionCapExceeded as exc:
            self._stage = CollectionStage.INTERRUPTED_RESUMABLE
            reason = exc.cap
            self._interrupt_reason = reason
            _write_progress(
                namespace,
                authorized,
                self._stage,
                clock=self._clock(),
                terminal=True,
                interrupt_reason=reason,
            )
            expected_count = _count_json_list(namespace / "expected_cells.json")
            family_count = _count_json_list(namespace / "events" / "accepted.json")
            return CollectionRunResult(
                collection_id=authorized.collection_id,
                stage=self._stage,
                collection_status=CollectionCapExceeded.COLLECTION_STATUS,
                collection_started=True,
                skipped_replay=False,
                accepted_family_count=family_count,
                expected_cell_count=expected_count,
                interrupt_reason=reason,
                ledger=ledger,
                manifest_path=self.manifest_path,
            )
        except RawProvenanceHashFailure:
            self._stage = CollectionStage.FAILED_INTEGRITY
            _write_progress(
                namespace,
                authorized,
                self._stage,
                clock=self._clock(),
                terminal=True,
                interrupt_reason="raw_hash_mismatch",
            )
            return CollectionRunResult(
                collection_id=authorized.collection_id,
                stage=self._stage,
                collection_status=CollectionStage.FAILED_INTEGRITY.value,
                collection_started=True,
                skipped_replay=False,
                accepted_family_count=_count_json_list(namespace / "events" / "accepted.json"),
                expected_cell_count=_count_json_list(namespace / "expected_cells.json"),
                interrupt_reason="raw_hash_mismatch",
                ledger=ledger,
                manifest_path=self.manifest_path,
            )

    def _verify_existing_hashes(self, namespace: Path, ledger: AppendOnlyLedger) -> None:
        for record in ledger.records():
            if not record.content_sha256 or not record.stable_raw_provenance_path:
                continue
            probe = probe_raw(
                namespace / Path(record.stable_raw_provenance_path),
                record.content_sha256,
            )
            if probe.exists and not probe.hash_matches:
                raise RawProvenanceHashFailure(
                    f"raw hash mismatch for {record.canonical_request_identity}"
                )

    def _discover_gamma(
        self,
        executor: BoundedGetExecutor,
        namespace: Path,
        pairs: tuple[tuple[str, str], ...],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for city, day in pairs:
            planned = PlannedGet(
                identity=f"gamma:{city}:{day}",
                provider=MARKET_PROVIDER,
                endpoint=GAMMA_ENDPOINT,
                day=day,
                params=gamma_search_params(city, day),
            )
            outcome = self._execute(executor, planned)
            payload = _payload_from_outcome(namespace, outcome.record)
            semantic = "http_network_failure"
            schema_status = None
            if payload is not None:
                schema = validate_gamma_search_payload(payload)
                semantic = classify_payload_semantic_class(schema)
                schema_status = schema.status
            row = {
                "city": city,
                "classification": outcome.record.result_classification.value,
                "content_sha256": outcome.record.content_sha256,
                "date": day,
                "identity": planned.identity,
                "schema_status": schema_status,
                "semantic_class": semantic,
                "skipped": outcome.skipped,
                "stable_raw_provenance_path": outcome.record.stable_raw_provenance_path,
            }
            assert_text_has_no_machine_roots(json.dumps(row, sort_keys=True))
            rows.append(row)
        return rows

    def _assemble_events(
        self,
        namespace: Path,
        gamma_rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        snapshots: list[ResearchSnapshot] = []
        extra_quarantine: list[dict[str, Any]] = []
        stations = catalog_stations()
        for row in gamma_rows:
            if row["classification"] not in {
                ResultClassification.SUCCESS.value,
                ResultClassification.VALID_EMPTY.value,
                ResultClassification.SKIPPED_ALREADY_COMPLETE.value,
            }:
                continue
            if row.get("semantic_class") not in {"schema_valid_eligible", "valid_empty"}:
                continue
            payload = _load_stable_raw(namespace, row.get("stable_raw_provenance_path"))
            if not isinstance(payload, dict):
                continue
            city = str(row["city"])
            day = str(row["date"])
            url = GAMMA_ENDPOINT
            for raw in _markets_from_search(payload):
                parsed = parse_gamma_market(
                    raw,
                    retrieved_url=url,
                    retrieved_at=self._clock(),
                    raw_path=row.get("stable_raw_provenance_path"),
                    content_sha256=row.get("content_sha256"),
                )
                built, quarantined = _snapshots_from_parsed(
                    parsed,
                    city=city,
                    day=day,
                    stations=stations,
                    stable_path=row.get("stable_raw_provenance_path"),
                    digest=row.get("content_sha256"),
                )
                snapshots.extend(built)
                extra_quarantine.extend(quarantined)
        accepted, group_quarantine = accept_event_groups(snapshots)
        families = _family_records(accepted, stations)
        quarantined = extra_quarantine + [_quarantine_dict(item) for item in group_quarantine]
        return families, quarantined

    def _plan_ecmwf(
        self,
        expected: tuple[ExpectedCell, ...],
        families: list[dict[str, Any]],
    ) -> tuple[list[PlannedGet], dict[str, list[dict[str, Any]]], tuple[ExpectedCell, ...]]:
        stations = catalog_stations()
        family_index = {row["event_family_id"]: row for row in families}
        plans: dict[str, PlannedGet] = {}
        mapping: dict[str, list[dict[str, Any]]] = {}
        updated: list[ExpectedCell] = []
        for cell in expected:
            family = family_index.get(cell.event_family_id)
            if family is None:
                updated.append(cell)
                continue
            matched = stations_for_city(cell.city, stations)
            station = next((row for row in matched if row.station_id == cell.station), None)
            if station is None:
                updated.append(cell)
                continue
            decision = decision_timestamp(cell.date, station.timezone_name, cell.checkpoint)
            choice = choose_ecmwf_run(decision_ts=decision, event_date=cell.date, station=station)
            run_param = None if choice is None else choice.run_param
            updated.append(
                ExpectedCell(
                    date=cell.date,
                    city=cell.city,
                    station=cell.station,
                    checkpoint=cell.checkpoint,
                    event_family_id=cell.event_family_id,
                    month=cell.month,
                    ecmwf_run_cycle=run_param,
                )
            )
            if choice is None:
                continue
            identity = f"ecmwf:{station.station_id}:{choice.run_param}"
            if identity not in plans:
                plans[identity] = PlannedGet(
                    identity=identity,
                    provider=FORECAST_PROVIDER,
                    endpoint=ECMWF_ENDPOINT,
                    day=cell.date,
                    params={
                        "hourly": ",".join(SINGLE_RUN_HOURLY_VARIABLES),
                        "latitude": station.latitude,
                        "longitude": station.longitude,
                        "models": SINGLE_RUN_MODEL,
                        "run": choice.run_param,
                        "temperature_unit": "celsius",
                        "timezone": station.timezone_name,
                    },
                )
            mapping.setdefault(identity, []).append(updated[-1].as_dict())
        ordered = tuple(plans.values())
        return list(ordered), mapping, tuple(updated)

    def _plan_clob(
        self,
        expected: tuple[ExpectedCell, ...],
        families: list[dict[str, Any]],
    ) -> tuple[list[PlannedGet], dict[str, list[dict[str, Any]]]]:
        del self
        return plan_clob_gets(expected, families)

    def _collect_gets(
        self,
        executor: BoundedGetExecutor,
        plans: list[PlannedGet],
        *,
        kind: str,
        namespace: Path,
    ) -> list[dict[str, Any]]:
        parsed: list[dict[str, Any]] = []
        for planned in plans:
            outcome = self._execute(executor, planned)
            payload = _payload_from_outcome(namespace, outcome.record)
            row: dict[str, Any] = {
                "classification": outcome.record.result_classification.value,
                "content_sha256": outcome.record.content_sha256,
                "identity": planned.identity,
                "kind": kind,
                "params": dict(planned.params),
                "skipped": outcome.skipped,
                "stable_raw_provenance_path": outcome.record.stable_raw_provenance_path,
            }
            if kind == "ecmwf":
                run_param = str(planned.params.get("run") or "")
                issued = _parse_run_param(run_param)
                available = None if issued is None else issued + availability_lag()
                if self._force_post_decision_inputs and available is not None:
                    available = datetime(2026, 3, 15, 12, tzinfo=UTC)
                schema_status = None
                if payload is not None:
                    schema_status = validate_single_run_payload(payload).status
                row.update(
                    {
                        "available_at": None if available is None else available.isoformat(),
                        "issued_at": None if issued is None else issued.isoformat(),
                        "model": FORECAST_MODEL,
                        "run_param": run_param,
                        "schema_status": schema_status,
                    }
                )
            else:
                row.update(parse_clob_points(payload))
            assert_text_has_no_machine_roots(json.dumps(row, sort_keys=True, default=str))
            parsed.append(row)
        return parsed

    def _select_and_observe(
        self,
        *,
        expected: tuple[ExpectedCell, ...],
        families: list[dict[str, Any]],
        ecmwf_parsed: list[dict[str, Any]],
        ecmwf_map: dict[str, list[dict[str, Any]]],
        clob_parsed: list[dict[str, Any]],
        clob_map: dict[str, list[dict[str, Any]]],
        namespace: Path,
        ledger: AppendOnlyLedger,
    ) -> list[DatasetObservation]:
        del self
        return assemble_dataset_observations(
            expected=expected,
            families=families,
            ecmwf_parsed=ecmwf_parsed,
            ecmwf_map=ecmwf_map,
            clob_parsed=clob_parsed,
            clob_map=clob_map,
            ecmwf_namespace=namespace,
            clob_namespace=namespace,
            ecmwf_ledger=ledger,
            clob_ledger=ledger,
        )

    def _execute(self, executor: BoundedGetExecutor, planned: PlannedGet) -> AttemptOutcome:
        outcome = execute_until_terminal(executor, planned)
        if not outcome.skipped:
            self._new_gets += 1
        return outcome


def _overlay_network_authorization(
    enforcement: BudgetEnforcement,
    *,
    network_authorized: bool,
) -> BudgetEnforcement:
    """Attach a network flag derived from persisted authorization verification."""
    return BudgetEnforcement(
        allowed=enforcement.allowed,
        status=enforcement.status,
        network_authorized=network_authorized,
        full_collection_start_allowed=enforcement.full_collection_start_allowed,
        theoretical_envelope_authorized=enforcement.theoretical_envelope_authorized,
        violated_caps=enforcement.violated_caps,
        estimate=enforcement.estimate,
        storage_preflight_ok=enforcement.storage_preflight_ok,
        detail=enforcement.detail,
    )


def execute_until_terminal(executor: BoundedGetExecutor, planned: PlannedGet) -> AttemptOutcome:
    while True:
        outcome = executor.execute(planned)
        if outcome.skipped:
            return outcome
        if is_retryable(outcome.record.result_classification) and not attempts_exhausted(
            outcome.record.attempt_number
        ):
            continue
        return outcome


def parse_clob_points(payload: Any) -> dict[str, Any]:
    schema_status = None
    points: list[dict[str, Any]] = []
    if payload is not None:
        schema = validate_prices_history_payload(payload)
        schema_status = schema.status
        if schema.status == "ok":
            try:
                for point in parse_price_history_points(payload):
                    points.append(
                        {
                            "observed_at": point.observed_at.isoformat(),
                            "price": point.price,
                        }
                    )
            except ProviderSchemaError:
                schema_status = "malformed"
    return {
        "points": points,
        "price_semantics": "DESCRIPTIVE_ONLY",
        "price_selection_rule": PRICE_SELECTION_RULE,
        "schema_status": schema_status,
    }


def parse_clob_collection_row(
    *,
    planned: PlannedGet,
    outcome: AttemptOutcome,
    namespace: Path,
) -> dict[str, Any]:
    payload = _payload_from_outcome(namespace, outcome.record)
    row: dict[str, Any] = {
        "classification": outcome.record.result_classification.value,
        "content_sha256": outcome.record.content_sha256,
        "identity": planned.identity,
        "kind": "clob",
        "params": dict(planned.params),
        "skipped": outcome.skipped,
        "stable_raw_provenance_path": outcome.record.stable_raw_provenance_path,
    }
    row.update(parse_clob_points(payload))
    assert_text_has_no_machine_roots(json.dumps(row, sort_keys=True, default=str))
    return row


def assemble_dataset_observations(
    *,
    expected: tuple[ExpectedCell, ...],
    families: list[dict[str, Any]],
    ecmwf_parsed: list[dict[str, Any]],
    ecmwf_map: dict[str, list[dict[str, Any]]],
    clob_parsed: list[dict[str, Any]],
    clob_map: dict[str, list[dict[str, Any]]],
    ecmwf_namespace: Path,
    clob_namespace: Path,
    ecmwf_ledger: AppendOnlyLedger,
    clob_ledger: AppendOnlyLedger,
) -> list[DatasetObservation]:
    family_index = {row["event_family_id"]: row for row in families}
    ecmwf_by_id = {row["identity"]: row for row in ecmwf_parsed}
    clob_by_id = {row["identity"]: row for row in clob_parsed}
    cell_to_ecmwf = _reverse_map(ecmwf_map)
    cell_to_clob = _reverse_map(clob_map)
    observations: list[DatasetObservation] = []
    stations = catalog_stations()
    for cell in expected:
        family = family_index.get(cell.event_family_id) or {}
        reasons: list[str] = []
        future_leakage = False
        ecmwf_id = cell_to_ecmwf.get(_cell_tuple(cell))
        clob_id = cell_to_clob.get(_cell_tuple(cell))
        ecmwf_row = None if ecmwf_id is None else ecmwf_by_id.get(ecmwf_id)
        clob_row = None if clob_id is None else clob_by_id.get(clob_id)
        matched = stations_for_city(cell.city, stations)
        station = next((row for row in matched if row.station_id == cell.station), None)
        timezone_name = family.get("timezone_name") or (station.timezone_name if station else "UTC")
        decision = decision_timestamp(cell.date, str(timezone_name), cell.checkpoint)
        forecast_ok = False
        run_cycle = cell.ecmwf_run_cycle
        if ecmwf_row is None:
            reasons.append(NO_VALID_FORECAST_BEFORE_DECISION)
        else:
            reasons.extend(_operational_reasons(ecmwf_row.get("classification")))
            available_raw = ecmwf_row.get("available_at")
            issued_raw = ecmwf_row.get("issued_at")
            run_param = str(ecmwf_row.get("run_param") or "")
            payload = _load_stable_raw(ecmwf_namespace, ecmwf_row.get("stable_raw_provenance_path"))
            schema_status = ecmwf_row.get("schema_status")
            if schema_status not in {"ok", None}:
                if schema_status in {"empty"}:
                    reasons.append(NO_VALID_FORECAST_BEFORE_DECISION)
                elif schema_status in {"malformed", "source_drift"}:
                    reasons.append(ResultClassification.SCHEMA_ERROR.value)
            candidates: list[ForecastCandidate] = []
            if issued_raw and available_raw and run_param:
                candidates.append(
                    ForecastCandidate(
                        issued_at=datetime.fromisoformat(str(issued_raw)),
                        available_at=datetime.fromisoformat(str(available_raw)),
                        run_param=run_param,
                        model=FORECAST_MODEL,
                    )
                )
            selected = select_forecast_at_or_before(candidates, decision) if candidates else None
            if selected is None:
                if candidates and all(row.available_at > decision for row in candidates):
                    future_leakage = True
                reasons.append(NO_VALID_FORECAST_BEFORE_DECISION)
            else:
                forecast_ok = schema_status in {None, "ok"} and isinstance(payload, dict)
                if isinstance(payload, dict):
                    coverage = evaluate_single_run_event_coverage(payload, event_date=cell.date)
                    if not coverage.usable:
                        forecast_ok = False
                        reasons.append(NO_VALID_FORECAST_BEFORE_DECISION)
                    if station is not None or matched:
                        parse_single_run_forecast(
                            payload,
                            station=station or matched[0],
                            issued_at=selected.issued_at,
                            request_url=ECMWF_ENDPOINT,
                        )
                run_cycle = selected.run_param
        has_price = False
        if clob_row is None:
            reasons.append(NO_PRE_DECISION_PRICE)
        else:
            reasons.extend(_operational_reasons(clob_row.get("classification")))
            classification = str(clob_row.get("classification") or "")
            schema_status = clob_row.get("schema_status")
            points_raw = clob_row.get("points") or []
            if classification == ResultClassification.VALID_EMPTY.value or schema_status == "empty":
                reasons.append(PRICE_HISTORY_EMPTY)
            elif schema_status in {"malformed", "source_drift"}:
                reasons.append(ResultClassification.SCHEMA_ERROR.value)
            else:
                points = [
                    PricePoint(
                        observed_at=datetime.fromisoformat(str(item["observed_at"])),
                        price=None if item.get("price") is None else float(item["price"]),
                    )
                    for item in points_raw
                    if isinstance(item, dict) and item.get("observed_at")
                ]
                chosen = select_price_at_or_before(points, decision)
                if chosen is None:
                    if points and all(point.observed_at > decision for point in points):
                        future_leakage = True
                        reasons.append(NO_PRE_DECISION_PRICE)
                    elif not points:
                        reasons.append(PRICE_HISTORY_EMPTY)
                    else:
                        reasons.append(NO_PRE_DECISION_PRICE)
                else:
                    has_price = True
        ecmwf_hash_ok = _hashes_ok(ecmwf_namespace, ecmwf_ledger, [ecmwf_id])
        clob_hash_ok = _hashes_ok(clob_namespace, clob_ledger, [clob_id])
        hash_ok = ecmwf_hash_ok and clob_hash_ok
        if not hash_ok:
            reasons.append("raw_hash_mismatch")
        unique_reasons = tuple(dict.fromkeys(reason for reason in reasons if reason))
        topology_valid = True
        has_settlement = bool(family.get("has_settlement"))
        usable = (
            forecast_ok
            and hash_ok
            and not future_leakage
            and topology_valid
            and NO_VALID_FORECAST_BEFORE_DECISION not in unique_reasons
        )
        observations.append(
            DatasetObservation(
                date=cell.date,
                city=cell.city,
                station=cell.station,
                checkpoint=cell.checkpoint,
                event_family_id=cell.event_family_id,
                month=cell.month,
                ecmwf_run_cycle=run_cycle,
                observed=True,
                usable=usable,
                has_settlement=has_settlement,
                scored=has_settlement,
                has_price_history=has_price,
                future_leakage=future_leakage,
                retrospective_substitution=False,
                raw_hash_ok=hash_ok,
                topology_valid=topology_valid,
                topology_reviewed_quarantine=False,
                missing_reasons=unique_reasons,
            )
        )
    return observations


def _write_progress(
    namespace: Path,
    authorized: AuthorizedManifest,
    stage: CollectionStage,
    *,
    clock: datetime,
    terminal: bool = False,
    interrupt_reason: str | None = None,
) -> None:
    payload = {
        "collection_id": authorized.collection_id,
        "interrupt_reason": interrupt_reason,
        "manifest_sha256": authorized.manifest_sha256,
        "stage": stage.value,
        "terminal": terminal
        or stage
        in {
            CollectionStage.COMPLETE,
            CollectionStage.INTERRUPTED_RESUMABLE,
            CollectionStage.FAILED_INTEGRITY,
        },
        "updated_at": clock.isoformat(),
    }
    _persist_json(namespace / "progress.json", payload)


def _persist_json(path: Path, payload: Any) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)
    assert_text_has_no_machine_roots(encoded)
    atomic_write_json(path, payload)


def _count_json_list(path: Path) -> int:
    if not path.is_file():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    return len(payload) if isinstance(payload, list) else 0


def _payload_from_outcome(namespace: Path, record: Any) -> Any | None:
    return _load_stable_raw(namespace, record.stable_raw_provenance_path)


def _load_stable_raw(namespace: Path, stable: str | None) -> Any | None:
    if not stable:
        return None
    runtime = namespace / Path(stable)
    if not runtime.is_file():
        return None
    return json.loads(runtime.read_text(encoding="utf-8"))


def _snapshots_from_parsed(
    parsed: ParsedGammaMarket,
    *,
    city: str,
    day: str,
    stations: tuple[Station, ...],
    stable_path: str | None,
    digest: str | None,
) -> tuple[list[ResearchSnapshot], list[dict[str, Any]]]:
    market = parsed.market
    quarantined: list[dict[str, Any]] = []
    snapshots: list[ResearchSnapshot] = []
    if not is_temperature_market_text(market.question, market.slug):
        quarantined.append(
            {
                "reason": "not a temperature market",
                "condition_id": market.condition_id,
                "city": city,
            }
        )
        return snapshots, quarantined
    if market.event_date != day or (market.city or "").strip().lower() != city:
        quarantined.append(
            {
                "reason": "discovered_outside_identity",
                "condition_id": market.condition_id,
                "city": city,
                "event_date": market.event_date,
            }
        )
        return snapshots, quarantined
    station, station_reason = resolve_research_station(market.station_icao, stations)
    if station is None:
        quarantined.append(
            {
                "reason": station_reason or "unknown station",
                "condition_id": market.condition_id,
                "city": city,
            }
        )
        return snapshots, quarantined
    resolved_flag = parsed.raw.get("resolved")
    settlement = parse_settlement_label(
        closed=market.closed,
        resolved=resolved_flag if isinstance(resolved_flag, bool) else None,
        outcomes=parsed.raw.get("outcomes"),
        outcome_prices=parsed.raw.get("outcomePrices") or parsed.raw.get("outcome_prices"),
    )
    yes_outcomes = [row for row in parsed.outcomes if row.outcome_label.lower() == "yes"]
    if not yes_outcomes:
        quarantined.append(
            {
                "reason": "YES token missing",
                "condition_id": market.condition_id,
                "city": city,
            }
        )
        return snapshots, quarantined
    decision = decision_timestamp(day, station.timezone_name, 1)
    for outcome in yes_outcomes:
        snapshots.append(
            ResearchSnapshot(
                condition_id=market.condition_id,
                market_id=market.market_id,
                token_id=outcome.token_id,
                city=market.city,
                station_icao=station.station_id,
                event_date=day,
                bucket_label=outcome.group_item_title or outcome.outcome_label,
                bucket_kind=outcome.bucket_kind,
                temperature_celsius_min=outcome.temperature_celsius_min,
                temperature_celsius_max=outcome.temperature_celsius_max,
                decision_ts=decision,
                market_probability=None,
                executable_entry_price=None,
                best_bid=None,
                best_ask=None,
                midpoint=None,
                spread=None,
                volume=None,
                liquidity=None,
                weather_issued_at=None,
                weather_available_at=None,
                forecast_daily_max_c=None,
                observation_max_so_far_c=None,
                observation_as_of=None,
                settlement_label=settlement.label,
                diagnostic_actual_max_c=None,
                provenance_urls=(market.provenance.request_url,),
                raw_paths=() if not stable_path else (stable_path,),
                content_hashes=() if not digest else (digest,),
                limitations=(
                    "GAMMA_SURVIVORSHIP_LIMITATION",
                    "HISTORICAL_UNIVERSE_COMPLETE=NOT_PROVEN",
                    "price_semantics=DESCRIPTIVE_ONLY",
                ),
                event_id=market.event_id,
                question=market.question,
                group_item_title=outcome.group_item_title,
                slug=market.slug,
                event_slug=market.event_slug,
                neg_risk_market_id=market.neg_risk_market_id,
                temperature_unit=outcome.temperature_unit,
                temperature_native_min=outcome.temperature_native_min,
                temperature_native_max=outcome.temperature_native_max,
                source_station_icao=market.station_icao,
            )
        )
    return snapshots, quarantined


def _family_records(
    accepted: tuple[ResearchSnapshot, ...],
    stations: tuple[Station, ...],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[ResearchSnapshot]] = {}
    for snap in accepted:
        grouped.setdefault(event_group_key(snap), []).append(snap)
    families: list[dict[str, Any]] = []
    for _key, members in grouped.items():
        representative = members[0]
        station, _reason = resolve_research_station(representative.station_icao, stations)
        timezone_name = station.timezone_name if station is not None else "UTC"
        yes_tokens = sorted({row.token_id for row in members})
        family_key = event_group_key(representative)
        families.append(
            {
                "canonical_event_key": list(family_key),
                "city": representative.city,
                "date": representative.event_date,
                "event_family_id": ":".join(family_key),
                "has_settlement": any(row.settlement_label is not None for row in members),
                "member_condition_ids": sorted({row.condition_id for row in members}),
                "station": representative.station_icao,
                "timezone_name": timezone_name,
                "yes_token_ids": yes_tokens,
            }
        )
    families.sort(key=lambda row: (str(row["date"]), str(row["city"]), str(row["event_family_id"])))
    return families


def _expected_cells(families: list[dict[str, Any]]) -> tuple[ExpectedCell, ...]:
    cells: list[ExpectedCell] = []
    for family in families:
        day = str(family["date"])
        city = str(family["city"] or "")
        station = str(family["station"] or "")
        family_id = str(family["event_family_id"])
        for lead in CHECKPOINTS:
            cells.append(
                ExpectedCell(
                    date=day,
                    city=city,
                    station=station,
                    checkpoint=lead,
                    event_family_id=family_id,
                    month=day[:7],
                    ecmwf_run_cycle=None,
                )
            )
    return tuple(cells)


def _quarantine_dict(record: QuarantineRecord) -> dict[str, Any]:
    payload = {
        "city": record.city,
        "condition_id": record.condition_id,
        "details": record.details,
        "event_date": record.event_date,
        "market_id": record.market_id,
        "reason": record.reason,
        "station_icao": record.station_icao,
        "token_id": record.token_id,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str)
    assert_text_has_no_machine_roots(encoded)
    return payload


def plan_to_dict(planned: PlannedGet) -> dict[str, Any]:
    return {
        "day": planned.day,
        "endpoint": planned.endpoint,
        "identity": planned.identity,
        "params": dict(planned.params),
        "provider": planned.provider,
    }


def _parse_run_param(run_param: str) -> datetime | None:
    if not run_param:
        return None
    try:
        parsed = datetime.strptime(run_param, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def _reverse_map(
    mapping: dict[str, list[dict[str, Any]]],
) -> dict[tuple[str, str, str, int, str], str]:
    reversed_map: dict[tuple[str, str, str, int, str], str] = {}
    for identity, cells in mapping.items():
        for cell in cells:
            reversed_map[
                (
                    str(cell["date"]),
                    str(cell["city"]),
                    str(cell["station"]),
                    int(cell["checkpoint"]),
                    str(cell["event_family_id"]),
                )
            ] = identity
    return reversed_map


def _cell_tuple(cell: ExpectedCell) -> tuple[str, str, str, int, str]:
    return (cell.date, cell.city, cell.station, cell.checkpoint, cell.event_family_id)


def _operational_reasons(classification: Any) -> list[str]:
    token = str(classification or "")
    if token in {
        ResultClassification.RATE_LIMITED.value,
        ResultClassification.TIMEOUT.value,
        ResultClassification.TLS_FAILURE.value,
        ResultClassification.TRANSIENT_TRANSPORT_FAILURE.value,
        ResultClassification.TRANSIENT_5XX.value,
        ResultClassification.HTTP_FAILURE.value,
        ResultClassification.INTERRUPTED_RESUMABLE.value,
        ResultClassification.SCHEMA_ERROR.value,
        ResultClassification.INELIGIBLE.value,
    }:
        return [token]
    return []


def _hashes_ok(namespace: Path, ledger: AppendOnlyLedger, identities: list[str | None]) -> bool:
    for identity in identities:
        if not identity:
            continue
        rows = [
            row
            for row in ledger.records_for(identity)
            if row.content_sha256 and row.stable_raw_provenance_path
        ]
        if not rows:
            continue
        latest = rows[-1]
        if latest.stable_raw_provenance_path is None or latest.content_sha256 is None:
            continue
        probe = probe_raw(
            namespace / Path(latest.stable_raw_provenance_path),
            latest.content_sha256,
        )
        if probe.exists and not probe.hash_matches:
            return False
    return True
