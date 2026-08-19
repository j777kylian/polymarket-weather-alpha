"""CLOB-only recovery overlay. Offline planning/authorization; fake GET transports in tests.

Derives recovery targets only from persisted parent ledger evidence. Manifest
creation, authorization, and execution are separate. No caller-controlled
boolean may enable networking.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_alpha.config.stations import Station
from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.models.timeutil import utc_now
from weather_alpha.phase35.full_collection.audit import ExpectedCell
from weather_alpha.phase35.full_collection.budget import (
    BudgetEnforcement,
    BudgetEstimate,
    DiskProbe,
    StaticDiskProbe,
)
from weather_alpha.phase35.full_collection.clob_contract import (
    canonical_clob_identity,
    clob_range_params,
    clob_window_timestamps,
)
from weather_alpha.phase35.full_collection.corpus import CorpusAssembly
from weather_alpha.phase35.full_collection.executor import (
    BoundedGetExecutor,
    CollectionCapExceeded,
    CollectionPreflightBlocked,
    PlannedGet,
)
from weather_alpha.phase35.full_collection.ledger import (
    AppendOnlyLedger,
    LedgerRecord,
    RawProvenanceHashFailure,
    ResultClassification,
)
from weather_alpha.phase35.full_collection.manifest import (
    canonical_json,
    payload_sha256,
    resolve_code_commit,
)
from weather_alpha.phase35.full_collection.orchestrator import (
    CollectionStage,
    assemble_dataset_observations,
    execute_until_terminal,
    parse_clob_collection_row,
    plan_to_dict,
)
from weather_alpha.phase35.full_collection.policy import (
    CLOB_ATTEMPT_CAP,
    CLOB_ENDPOINT,
    CLOB_FIDELITY_MINUTES,
    CLOB_WINDOW_RULE_VERSION,
    CONCURRENCY,
    GLOBAL_GET_ATTEMPT_CAP,
    HASH_ALGORITHM,
    HTTP_METHOD,
    MAX_ATTEMPTS_PER_IDENTITY,
    MAX_RETRIES,
    PARENT_CLOB_HTTP_FAILURE_SCALE,
    PARSER_SCHEMA_VERSION,
    PREFLIGHT_OK,
    PRICE_PROVIDER,
    RECOVERY_AUTHORIZATION_SCHEMA_VERSION,
    RECOVERY_RAW_STORAGE_NAMESPACE,
    RECOVERY_SCHEMA_VERSION,
    RECOVERY_SCOPE_CLOB_ONLY,
    REQUEST_BUDGET_REDESIGN_REQUIRED,
    REQUEST_POLICY_VERSION,
    RETRY_AFTER_CAP_SECONDS,
    RETRY_MODE,
    RETRY_ONLY,
    RETRYABLE_HTTP_STATUSES,
    STORAGE_HARD_CAP_BYTES,
    STORAGE_PREFLIGHT_MIN_BYTES,
    TIMEOUT_SECONDS,
    YES_PENDING_FINAL_REVIEW,
)
from weather_alpha.phase35.full_collection.provenance import (
    assert_text_has_no_machine_roots,
    atomic_write_json,
    probe_raw,
)
from weather_alpha.phase35.full_collection.schedule import catalog_stations, stations_for_city

RECOVERY_AUTHORIZATION_REQUIRED_FIELDS: tuple[str, ...] = (
    "AUTHORIZED_AT",
    "RECOVERY_AUTHORIZATION_SCHEMA_VERSION",
    "RECOVERY_CODE_COMMIT",
    "RECOVERY_ID",
    "RECOVERY_MANIFEST_SHA256",
    "REQUEST_POLICY_VERSION",
)


class RecoveryAuthorizationError(ValueError):
    """Fail-closed refusal for absent/invalid/mismatched recovery authorization."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ClobRecoveryTarget:
    city: str
    date: str
    token: str
    start_ts: int
    end_ts: int
    fidelity: int
    identity: str
    parent_identity: str
    timezone_name: str
    cells: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ClobRecoveryDerivation:
    targets: tuple[ClobRecoveryTarget, ...]
    gamma_recovery_identities: int = 0
    ecmwf_recovery_identities: int = 0
    count_mismatch_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryManifestCreateResult:
    status: str
    written: bool
    recovery_id: str | None
    manifest_sha256: str | None
    payload: dict[str, Any]
    derivation: ClobRecoveryDerivation

    def as_dict(self) -> dict[str, Any]:
        return {
            "CLOB_RECOVERY_IDENTITIES": len(self.derivation.targets),
            "ECMWF_RECOVERY_IDENTITIES": 0,
            "GAMMA_RECOVERY_IDENTITIES": 0,
            "PROVIDER_REQUESTS": 0,
            "manifest_sha256": self.manifest_sha256,
            "payload": self.payload,
            "recovery_id": self.recovery_id,
            "status": self.status,
            "written": self.written,
        }


@dataclass(frozen=True, slots=True)
class RecoveryAuthorizationReceipt:
    recovery_id: str
    recovery_manifest_sha256: str
    recovery_code_commit: str
    request_policy_version: str
    authorized_at: str
    schema_version: str

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "AUTHORIZED_AT": self.authorized_at,
            "RECOVERY_AUTHORIZATION_SCHEMA_VERSION": self.schema_version,
            "RECOVERY_CODE_COMMIT": self.recovery_code_commit,
            "RECOVERY_ID": self.recovery_id,
            "RECOVERY_MANIFEST_SHA256": self.recovery_manifest_sha256,
            "REQUEST_POLICY_VERSION": self.request_policy_version,
        }
        assert_text_has_no_machine_roots(str(payload))
        return payload


@dataclass(frozen=True, slots=True)
class AuthorizedRecoveryManifest:
    path: Path
    payload: dict[str, Any]
    recovery_id: str
    manifest_sha256: str
    code_commit: str
    request_policy_version: str
    network_authorized: bool
    planned_gets: tuple[PlannedGet, ...]


@dataclass(frozen=True, slots=True)
class RecoveryRunResult:
    recovery_id: str
    stage: CollectionStage
    collection_status: str
    skipped_replay: bool
    clob_recovery_identities: int
    interrupt_reason: str | None
    ledger: AppendOnlyLedger

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "clob_recovery_identities": self.clob_recovery_identities,
            "collection_status": self.collection_status,
            "interrupt_reason": self.interrupt_reason,
            "recovery_id": self.recovery_id,
            "skipped_replay": self.skipped_replay,
            "stage": self.stage.value,
        }
        assert_text_has_no_machine_roots(json.dumps(payload, sort_keys=True))
        return payload


def derive_clob_recovery_targets(parent_namespace: Path) -> ClobRecoveryDerivation:
    ledger = AppendOnlyLedger(parent_namespace / "ledger.jsonl")
    terminals = _terminal_records(ledger)
    cell_map = _load_object_mapping(parent_namespace / "plans" / "clob_cell_map.json")
    families = _load_object_list(parent_namespace / "events" / "accepted.json")
    stations = catalog_stations()
    targets: list[ClobRecoveryTarget] = []
    for identity, record in sorted(terminals.items(), key=lambda item: item[0]):
        if record.provider != PRICE_PROVIDER:
            continue
        if record.result_classification is not ResultClassification.HTTP_FAILURE:
            continue
        params = dict(record.normalized_request_parameters)
        token = str(params.get("market") or "")
        if not token:
            continue
        cells = tuple(cell_map.get(identity) or ())
        city, day = _city_date_from_parent(identity, cells)
        timezone_name = _timezone_from_families(families, city=city, day=day, stations=stations)
        start_ts, end_ts = clob_window_timestamps(day, timezone_name)
        fidelity = int(params.get("fidelity") or CLOB_FIDELITY_MINUTES)
        if fidelity != CLOB_FIDELITY_MINUTES:
            fidelity = CLOB_FIDELITY_MINUTES
        corrected = canonical_clob_identity(
            market=token, start_ts=start_ts, end_ts=end_ts, fidelity=fidelity
        )
        targets.append(
            ClobRecoveryTarget(
                city=city,
                date=day,
                token=token,
                start_ts=start_ts,
                end_ts=end_ts,
                fidelity=fidelity,
                identity=corrected,
                parent_identity=identity,
                timezone_name=timezone_name,
                cells=cells,
            )
        )
    mismatch = None
    if len(targets) != PARENT_CLOB_HTTP_FAILURE_SCALE:
        mismatch = (
            f"derived {len(targets)} CLOB HTTP_FAILURE identities; "
            f"preserved parent scale is {PARENT_CLOB_HTTP_FAILURE_SCALE}"
        )
    return ClobRecoveryDerivation(
        targets=tuple(targets),
        gamma_recovery_identities=0,
        ecmwf_recovery_identities=0,
        count_mismatch_reason=mismatch,
    )


def estimate_clob_recovery_budget(identity_count: int) -> BudgetEstimate:
    clob = int(identity_count)
    per_identity = MAX_ATTEMPTS_PER_IDENTITY
    theoretical_clob = clob * per_identity
    clob_max = min(theoretical_clob, CLOB_ATTEMPT_CAP)
    clob_reserve = CLOB_ATTEMPT_CAP - clob
    return BudgetEstimate(
        gamma_identities=0,
        clob_identities=clob,
        ecmwf_logical_identities=0,
        gamma_initial_attempts=0,
        clob_initial_attempts=clob,
        ecmwf_initial_attempts=0,
        initial_total_attempts=clob,
        gamma_retry_reserve=0,
        ecmwf_retry_reserve=0,
        clob_retry_reserve=clob_reserve,
        gamma_max_attempts=0,
        clob_max_attempts=clob_max,
        ecmwf_max_attempts=0,
        global_max_attempts=min(clob_max, GLOBAL_GET_ATTEMPT_CAP),
        theoretical_gamma_attempts=0,
        theoretical_ecmwf_attempts=0,
        theoretical_clob_attempts=theoretical_clob,
        theoretical_global_attempts=theoretical_clob,
        theoretical_envelope_authorized=theoretical_clob <= CLOB_ATTEMPT_CAP
        and theoretical_clob <= GLOBAL_GET_ATTEMPT_CAP,
        max_attempts_per_identity=per_identity,
        planning_baseline_ecmwf_logical=0,
        computed_ecmwf_logical=0,
    )


def create_clob_recovery_manifest(
    *,
    destination: Path,
    parent_collection_root: Path,
    parent_collection_id: str,
    parent_manifest_path: Path | None = None,
    code_commit: str | None = None,
    created_at: datetime | None = None,
    disk: DiskProbe | None = None,
) -> RecoveryManifestCreateResult:
    del disk
    created = created_at or utc_now()
    commit = resolve_code_commit(code_commit)
    parent_namespace = parent_collection_root / parent_collection_id
    progress = _load_object(parent_namespace / "progress.json")
    if str(progress.get("collection_id") or "") != parent_collection_id:
        raise ValueError("parent progress collection_id does not match parent_collection_id")
    parent_manifest_sha256 = str(progress.get("manifest_sha256") or "")
    parent_code_commit = "unknown"
    if parent_manifest_path is not None:
        parent_payload = _load_object(parent_manifest_path)
        digest = payload_sha256(parent_payload)
        if parent_manifest_sha256 and digest != parent_manifest_sha256:
            raise ValueError("parent manifest SHA-256 does not match progress.manifest_sha256")
        parent_code_commit = str(parent_payload.get("CODE_COMMIT") or parent_code_commit)
        if not parent_manifest_sha256:
            parent_manifest_sha256 = digest
    derivation = derive_clob_recovery_targets(parent_namespace)
    planned: list[PlannedGet] = []
    cell_map: dict[str, list[dict[str, Any]]] = {}
    for target in derivation.targets:
        params = clob_range_params(target.token, target.start_ts, target.end_ts, target.fidelity)
        planned.append(
            PlannedGet(
                identity=target.identity,
                provider=PRICE_PROVIDER,
                endpoint=CLOB_ENDPOINT,
                day=target.date,
                params=params,
            )
        )
        cell_map[target.identity] = [dict(row) for row in target.cells]
    recovery_id = _derive_recovery_id(
        parent_collection_id=parent_collection_id,
        parent_manifest_sha256=parent_manifest_sha256,
        identities=tuple(row.identity for row in planned),
        code_commit=commit,
        created_at=created,
    )
    estimate = estimate_clob_recovery_budget(len(planned))
    if estimate.clob_max_attempts > CLOB_ATTEMPT_CAP or estimate.clob_retry_reserve < 0:
        raise ValueError("CLOB recovery would raise or exceed the frozen CLOB attempt cap")
    enforcement = _recovery_enforcement(estimate, network_authorized=False)
    payload: dict[str, Any] = {
        "CELL_MAP": cell_map,
        "CLOB_ENDPOINT": CLOB_ENDPOINT,
        "CLOB_IDENTITY_COUNT": len(planned),
        "CLOB_IDENTITY_COUNT_NOTE": derivation.count_mismatch_reason,
        "CLOB_RECOVERY_IDENTITIES": len(planned),
        "CLOB_REQUEST_POLICY_VERSION": REQUEST_POLICY_VERSION,
        "CLOB_WINDOW_RULE_VERSION": CLOB_WINDOW_RULE_VERSION,
        "CREATED_AT": created.isoformat(),
        "ECMWF_RECOVERY_IDENTITIES": 0,
        "GAMMA_RECOVERY_IDENTITIES": 0,
        "HASH_ALGORITHM": HASH_ALGORITHM,
        "PARENT_CODE_COMMIT": parent_code_commit,
        "PARENT_COLLECTION_ID": parent_collection_id,
        "PARENT_MANIFEST_SHA256": parent_manifest_sha256,
        "PARSER_SCHEMA_VERSION": PARSER_SCHEMA_VERSION,
        "PLANNED_GETS": [plan_to_dict(row) for row in planned],
        "PREFLIGHT": enforcement.as_dict(),
        "PRICE_PROVIDER": PRICE_PROVIDER,
        "RAW_STORAGE_NAMESPACE": RECOVERY_RAW_STORAGE_NAMESPACE,
        "RECOVERY_CODE_COMMIT": commit,
        "RECOVERY_ID": recovery_id,
        "RECOVERY_SCHEMA_VERSION": RECOVERY_SCHEMA_VERSION,
        "RECOVERY_SCOPE": RECOVERY_SCOPE_CLOB_ONLY,
        "REQUEST_CAPS": {
            "clob_attempts": CLOB_ATTEMPT_CAP,
            "ecmwf_attempts": 0,
            "gamma_attempts": 0,
            "global_get_attempts": min(CLOB_ATTEMPT_CAP, GLOBAL_GET_ATTEMPT_CAP),
        },
        "REQUEST_POLICY": {
            "concurrency": CONCURRENCY,
            "http_method": HTTP_METHOD,
            "timeout_seconds": TIMEOUT_SECONDS,
            "version": REQUEST_POLICY_VERSION,
        },
        "RETRY_POLICY": {
            "max_attempts_per_identity": MAX_ATTEMPTS_PER_IDENTITY,
            "max_retries": MAX_RETRIES,
            "retry_after_cap_seconds": RETRY_AFTER_CAP_SECONDS,
            "retry_mode": RETRY_MODE,
            "retry_only": list(RETRY_ONLY),
            "retryable_http_statuses": sorted(RETRYABLE_HTTP_STATUSES),
        },
        "price_semantics": "DESCRIPTIVE_ONLY",
    }
    encoded = canonical_json(payload)
    assert_text_has_no_machine_roots(encoded)
    if destination.is_file():
        raise ValueError(f"immutable recovery manifest already exists: {destination.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = atomic_write_json(destination, payload)
    del digest
    return RecoveryManifestCreateResult(
        status=PREFLIGHT_OK,
        written=True,
        recovery_id=recovery_id,
        manifest_sha256=payload_sha256(payload),
        payload=payload,
        derivation=derivation,
    )


def inspect_recovery_manifest(path: Path, *, expected_code_commit: str) -> dict[str, Any]:
    if not path.is_file():
        raise RecoveryAuthorizationError("missing", f"recovery manifest not found: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecoveryAuthorizationError("invalid", "recovery manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RecoveryAuthorizationError("invalid", "recovery manifest must be a JSON object")
    if str(payload.get("RECOVERY_SCHEMA_VERSION") or "") != RECOVERY_SCHEMA_VERSION:
        raise RecoveryAuthorizationError("invalid", "recovery schema is not the frozen contract")
    if str(payload.get("RECOVERY_SCOPE") or "") != RECOVERY_SCOPE_CLOB_ONLY:
        raise RecoveryAuthorizationError("scope_mismatch", "recovery scope is not CLOB_ONLY")
    gamma_ids = payload.get("GAMMA_RECOVERY_IDENTITIES")
    if gamma_ids is None or int(gamma_ids) != 0:
        raise RecoveryAuthorizationError("scope_mismatch", "GAMMA recovery identities must be 0")
    ecmwf_ids = payload.get("ECMWF_RECOVERY_IDENTITIES")
    if ecmwf_ids is None or int(ecmwf_ids) != 0:
        raise RecoveryAuthorizationError("scope_mismatch", "ECMWF recovery identities must be 0")
    if str(payload.get("RECOVERY_CODE_COMMIT") or "") != expected_code_commit:
        raise RecoveryAuthorizationError(
            "code_mismatch", "immutable RECOVERY_CODE_COMMIT does not match expected commit"
        )
    policy = payload.get("REQUEST_POLICY")
    if not isinstance(policy, dict) or str(policy.get("version")) != REQUEST_POLICY_VERSION:
        raise RecoveryAuthorizationError(
            "policy_mismatch", "REQUEST_POLICY version is not the frozen v2 contract"
        )
    if str(payload.get("CLOB_REQUEST_POLICY_VERSION") or "") != REQUEST_POLICY_VERSION:
        raise RecoveryAuthorizationError(
            "policy_mismatch", "CLOB_REQUEST_POLICY_VERSION is not request-policy-v2"
        )
    caps = payload.get("REQUEST_CAPS")
    if not isinstance(caps, dict) or int(caps.get("clob_attempts") or -1) != CLOB_ATTEMPT_CAP:
        raise RecoveryAuthorizationError("policy_mismatch", "CLOB attempt cap is not frozen at 600")
    planned = payload.get("PLANNED_GETS")
    if not isinstance(planned, list):
        raise RecoveryAuthorizationError("invalid", "PLANNED_GETS missing")
    if int(payload.get("CLOB_RECOVERY_IDENTITIES") or -1) != len(planned):
        raise RecoveryAuthorizationError("invalid", "CLOB_RECOVERY_IDENTITIES does not match plan")
    return payload


def create_clob_recovery_authorization_receipt(
    *,
    manifest_path: Path,
    destination: Path,
    expected_code_commit: str,
    authorized_at: datetime | None = None,
) -> RecoveryAuthorizationReceipt:
    payload = inspect_recovery_manifest(manifest_path, expected_code_commit=expected_code_commit)
    if destination.resolve() == manifest_path.resolve():
        raise ValueError(
            "authorization receipt destination must not overwrite the recovery manifest"
        )
    if destination.is_file():
        raise ValueError(
            f"immutable recovery authorization receipt already exists: {destination.name}"
        )
    receipt = RecoveryAuthorizationReceipt(
        recovery_id=str(payload["RECOVERY_ID"]),
        recovery_manifest_sha256=payload_sha256(payload),
        recovery_code_commit=str(payload["RECOVERY_CODE_COMMIT"]),
        request_policy_version=str(payload["CLOB_REQUEST_POLICY_VERSION"]),
        authorized_at=(authorized_at or utc_now()).isoformat(),
        schema_version=RECOVERY_AUTHORIZATION_SCHEMA_VERSION,
    )
    atomic_write_json(destination, receipt.as_dict())
    return receipt


def load_authorized_recovery_manifest(
    path: Path,
    *,
    expected_code_commit: str,
    authorization_path: Path,
) -> AuthorizedRecoveryManifest:
    payload = inspect_recovery_manifest(path, expected_code_commit=expected_code_commit)
    receipt = _load_recovery_receipt(authorization_path)
    digest = payload_sha256(payload)
    if receipt.recovery_id != str(payload["RECOVERY_ID"]):
        raise RecoveryAuthorizationError(
            "collection_id_mismatch",
            "authorization receipt RECOVERY_ID does not match the recovery manifest",
        )
    if receipt.recovery_manifest_sha256 != digest:
        raise RecoveryAuthorizationError(
            "manifest_sha_mismatch",
            "authorization receipt RECOVERY_MANIFEST_SHA256 does not match the recomputed digest",
        )
    if receipt.recovery_code_commit != str(payload["RECOVERY_CODE_COMMIT"]):
        raise RecoveryAuthorizationError(
            "code_mismatch",
            "authorization receipt RECOVERY_CODE_COMMIT does not match the recovery manifest",
        )
    if receipt.request_policy_version != REQUEST_POLICY_VERSION:
        raise RecoveryAuthorizationError(
            "policy_mismatch",
            "authorization receipt REQUEST_POLICY_VERSION does not match request-policy-v2",
        )
    planned = tuple(
        _planned_from_dict(row) for row in payload["PLANNED_GETS"] if isinstance(row, dict)
    )
    return AuthorizedRecoveryManifest(
        path=path,
        payload=payload,
        recovery_id=str(payload["RECOVERY_ID"]),
        manifest_sha256=digest,
        code_commit=str(payload["RECOVERY_CODE_COMMIT"]),
        request_policy_version=REQUEST_POLICY_VERSION,
        network_authorized=True,
        planned_gets=planned,
    )


class ClobRecoveryService:
    """Execute one authorized, CLOB-only recovery. GET-only."""

    def __init__(
        self,
        *,
        manifest_path: Path,
        authorization_path: Path,
        recovery_root: Path,
        parent_collection_root: Path,
        http: ReadOnlyHttpClient,
        expected_code_commit: str,
        enforcement: BudgetEnforcement | None = None,
        disk: DiskProbe | None = None,
        sleeper: Any = None,
        clock: Any = None,
        global_attempt_cap: int | None = None,
    ) -> None:
        self.manifest_path = manifest_path
        self.authorization_path = authorization_path
        self._recovery_root = recovery_root
        self._parent_collection_root = parent_collection_root
        self._http = http
        self._expected_code_commit = expected_code_commit
        self._enforcement_override = enforcement
        self._disk = disk or StaticDiskProbe(
            free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0
        )
        self._sleeper = sleeper or (lambda _seconds: None)
        self._clock = clock or utc_now
        self._global_attempt_cap = global_attempt_cap
        self._new_gets = 0

    def run(self) -> RecoveryRunResult:
        authorized = load_authorized_recovery_manifest(
            self.manifest_path,
            authorization_path=self.authorization_path,
            expected_code_commit=self._expected_code_commit,
        )
        parent_id = str(authorized.payload["PARENT_COLLECTION_ID"])
        parent_namespace = self._parent_collection_root / parent_id
        if parent_namespace.resolve() == (self._recovery_root / authorized.recovery_id).resolve():
            raise RecoveryAuthorizationError(
                "scope_mismatch", "recovery namespace must be distinct from the parent collection"
            )
        namespace = self._recovery_root / authorized.recovery_id
        namespace.mkdir(parents=True, exist_ok=True)
        ledger = AppendOnlyLedger(namespace / "ledger.jsonl")
        estimate = estimate_clob_recovery_budget(len(authorized.planned_gets))
        base = self._enforcement_override or _recovery_enforcement(
            estimate, network_authorized=False, disk=self._disk, storage_root=namespace
        )
        enforcement = BudgetEnforcement(
            allowed=base.allowed,
            status=base.status,
            network_authorized=authorized.network_authorized,
            full_collection_start_allowed=base.full_collection_start_allowed,
            theoretical_envelope_authorized=base.theoretical_envelope_authorized,
            violated_caps=base.violated_caps,
            estimate=base.estimate,
            storage_preflight_ok=base.storage_preflight_ok,
            detail=base.detail,
        )
        if not enforcement.allowed:
            raise CollectionPreflightBlocked(enforcement.status)
        if not enforcement.network_authorized:
            raise CollectionPreflightBlocked(
                enforcement.full_collection_start_allowed or enforcement.status
            )
        executor = BoundedGetExecutor(
            collection_id=authorized.recovery_id,
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
            _verify_existing_hashes(namespace, ledger)
            _write_recovery_progress(
                namespace,
                authorized,
                CollectionStage.CLOB_COLLECTION,
                clock=self._clock(),
            )
            parsed: list[dict[str, Any]] = []
            for planned in authorized.planned_gets:
                outcome = execute_until_terminal(executor, planned)
                if not outcome.skipped:
                    self._new_gets += 1
                parsed.append(
                    parse_clob_collection_row(planned=planned, outcome=outcome, namespace=namespace)
                )
            _persist_json(namespace / "parsed" / "clob.json", parsed)
            _persist_json(
                namespace / "plans" / "clob.json",
                [plan_to_dict(row) for row in authorized.planned_gets],
            )
            _persist_json(
                namespace / "plans" / "clob_cell_map.json",
                authorized.payload.get("CELL_MAP") or {},
            )
            _write_recovery_progress(
                namespace,
                authorized,
                CollectionStage.COMPLETE,
                clock=self._clock(),
                terminal=True,
            )
            return RecoveryRunResult(
                recovery_id=authorized.recovery_id,
                stage=CollectionStage.COMPLETE,
                collection_status=CollectionStage.COMPLETE.value,
                skipped_replay=self._new_gets == 0 and bool(ledger.records()),
                clob_recovery_identities=len(authorized.planned_gets),
                interrupt_reason=None,
                ledger=ledger,
            )
        except CollectionCapExceeded as exc:
            _write_recovery_progress(
                namespace,
                authorized,
                CollectionStage.INTERRUPTED_RESUMABLE,
                clock=self._clock(),
                terminal=True,
                interrupt_reason=exc.cap,
            )
            return RecoveryRunResult(
                recovery_id=authorized.recovery_id,
                stage=CollectionStage.INTERRUPTED_RESUMABLE,
                collection_status=CollectionCapExceeded.COLLECTION_STATUS,
                skipped_replay=False,
                clob_recovery_identities=len(authorized.planned_gets),
                interrupt_reason=exc.cap,
                ledger=ledger,
            )
        except RawProvenanceHashFailure:
            _write_recovery_progress(
                namespace,
                authorized,
                CollectionStage.FAILED_INTEGRITY,
                clock=self._clock(),
                terminal=True,
                interrupt_reason="raw_hash_mismatch",
            )
            return RecoveryRunResult(
                recovery_id=authorized.recovery_id,
                stage=CollectionStage.FAILED_INTEGRITY,
                collection_status=CollectionStage.FAILED_INTEGRITY.value,
                skipped_replay=False,
                clob_recovery_identities=len(authorized.planned_gets),
                interrupt_reason="raw_hash_mismatch",
                ledger=ledger,
            )


def merge_parent_and_recovery(
    *,
    parent_collection_root: Path,
    parent_collection_id: str,
    recovery_root: Path,
    recovery_id: str,
) -> CorpusAssembly:
    parent_namespace = parent_collection_root / parent_collection_id
    recovery_namespace = recovery_root / recovery_id
    expected_payload = _load_list(parent_namespace / "expected_cells.json")
    expected = tuple(
        ExpectedCell.from_dict(row) for row in expected_payload if isinstance(row, dict)
    )
    families = _load_object_list(parent_namespace / "events" / "accepted.json")
    ecmwf_parsed = _load_object_list(parent_namespace / "parsed" / "ecmwf.json")
    ecmwf_map = _load_object_mapping(parent_namespace / "plans" / "ecmwf_cell_map.json")
    clob_parsed = _load_object_list(recovery_namespace / "parsed" / "clob.json")
    clob_map = _load_object_mapping(recovery_namespace / "plans" / "clob_cell_map.json")
    parent_ledger = AppendOnlyLedger(parent_namespace / "ledger.jsonl")
    recovery_ledger = AppendOnlyLedger(recovery_namespace / "ledger.jsonl")
    observations = assemble_dataset_observations(
        expected=expected,
        families=families,
        ecmwf_parsed=ecmwf_parsed,
        ecmwf_map=ecmwf_map,
        clob_parsed=clob_parsed,
        clob_map=clob_map,
        ecmwf_namespace=parent_namespace,
        clob_namespace=recovery_namespace,
        ecmwf_ledger=parent_ledger,
        clob_ledger=recovery_ledger,
    )
    _copy_artifact(
        parent_namespace / "expected_cells.json",
        recovery_namespace / "expected_cells.json",
    )
    _copy_artifact(
        parent_namespace / "events" / "accepted.json",
        recovery_namespace / "events" / "accepted.json",
    )
    quarantined = parent_namespace / "events" / "quarantined.json"
    if quarantined.is_file():
        _copy_artifact(quarantined, recovery_namespace / "events" / "quarantined.json")
    _persist_json(recovery_namespace / "observations.json", [row.as_dict() for row in observations])
    _persist_json(
        recovery_namespace / "selections" / "pit.json",
        [row.as_dict() for row in observations],
    )
    recovery_progress = _load_object(recovery_namespace / "progress.json")
    parent_progress = _load_object(parent_namespace / "progress.json")
    lineage = {
        "CLOB_WINDOW_RULE_VERSION": CLOB_WINDOW_RULE_VERSION,
        "PARENT_CODE_COMMIT": str(
            recovery_progress.get("parent_code_commit")
            or parent_progress.get("code_commit")
            or "unknown"
        ),
        "PARENT_COLLECTION_ID": parent_collection_id,
        "PARENT_MANIFEST_SHA256": str(
            recovery_progress.get("parent_manifest_sha256")
            or parent_progress.get("manifest_sha256")
            or ""
        ),
        "RECOVERY_CODE_COMMIT": str(recovery_progress.get("recovery_code_commit") or ""),
        "RECOVERY_ID": recovery_id,
        "RECOVERY_MANIFEST_SHA256": str(recovery_progress.get("manifest_sha256") or ""),
        "RECOVERY_SCOPE": RECOVERY_SCOPE_CLOB_ONLY,
    }
    _persist_json(recovery_namespace / "recovery_lineage.json", lineage)
    quarantine_payload = _load_object_list(recovery_namespace / "events" / "quarantined.json")
    return CorpusAssembly(
        collection_id=recovery_id,
        expected=expected,
        observations=tuple(observations),
        quarantine=tuple(quarantine_payload),
    )


def _derive_recovery_id(
    *,
    parent_collection_id: str,
    parent_manifest_sha256: str,
    identities: tuple[str, ...],
    code_commit: str,
    created_at: datetime,
) -> str:
    material = {
        "code_commit": code_commit,
        "created_at": created_at.isoformat(),
        "identities": list(identities),
        "parent_collection_id": parent_collection_id,
        "parent_manifest_sha256": parent_manifest_sha256,
        "scope": RECOVERY_SCOPE_CLOB_ONLY,
        "window_rule": CLOB_WINDOW_RULE_VERSION,
    }
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()[:12]
    return f"phase35-clob-recovery-{digest}"


def _recovery_enforcement(
    estimate: BudgetEstimate,
    *,
    network_authorized: bool,
    disk: DiskProbe | None = None,
    storage_root: Path | None = None,
) -> BudgetEnforcement:
    storage_ok = True
    violated: list[str] = []
    detail: list[str] = []
    if estimate.clob_max_attempts > CLOB_ATTEMPT_CAP:
        violated.append("clob")
        detail.append("CLOB recovery max attempts exceed frozen cap")
    if disk is not None:
        root = storage_root or Path("data/phase35/historical/recoveries")
        free = disk.free_bytes(root)
        used = disk.used_bytes(root)
        if free < STORAGE_PREFLIGHT_MIN_BYTES:
            storage_ok = False
            violated.append("storage_preflight")
            detail.append(
                f"free bytes {free} below preflight minimum {STORAGE_PREFLIGHT_MIN_BYTES}"
            )
        if used >= STORAGE_HARD_CAP_BYTES:
            storage_ok = False
            violated.append("storage_hard_cap")
            detail.append(f"used bytes {used} at or above hard cap {STORAGE_HARD_CAP_BYTES}")
    allowed = not violated
    return BudgetEnforcement(
        allowed=allowed,
        status=PREFLIGHT_OK if allowed else REQUEST_BUDGET_REDESIGN_REQUIRED,
        network_authorized=network_authorized,
        full_collection_start_allowed=YES_PENDING_FINAL_REVIEW if allowed else "NO",
        theoretical_envelope_authorized=estimate.theoretical_envelope_authorized,
        violated_caps=tuple(violated),
        estimate=estimate,
        storage_preflight_ok=storage_ok,
        detail=tuple(detail),
    )


def _load_recovery_receipt(path: Path) -> RecoveryAuthorizationReceipt:
    if not path.is_file():
        raise RecoveryAuthorizationError(
            "missing_authorization", f"recovery authorization receipt not found: {path.name}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecoveryAuthorizationError(
            "invalid_authorization", "recovery authorization receipt is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RecoveryAuthorizationError(
            "invalid_authorization", "recovery authorization receipt must be a JSON object"
        )
    if str(payload.get("RECOVERY_AUTHORIZATION_SCHEMA_VERSION") or "") != (
        RECOVERY_AUTHORIZATION_SCHEMA_VERSION
    ):
        raise RecoveryAuthorizationError(
            "invalid_authorization", "recovery authorization schema is not the frozen contract"
        )
    if any(key not in payload for key in RECOVERY_AUTHORIZATION_REQUIRED_FIELDS):
        raise RecoveryAuthorizationError(
            "invalid_authorization", "recovery authorization receipt is missing required fields"
        )
    return RecoveryAuthorizationReceipt(
        recovery_id=str(payload["RECOVERY_ID"]),
        recovery_manifest_sha256=str(payload["RECOVERY_MANIFEST_SHA256"]),
        recovery_code_commit=str(payload["RECOVERY_CODE_COMMIT"]),
        request_policy_version=str(payload["REQUEST_POLICY_VERSION"]),
        authorized_at=str(payload["AUTHORIZED_AT"]),
        schema_version=str(payload["RECOVERY_AUTHORIZATION_SCHEMA_VERSION"]),
    )


def _planned_from_dict(row: dict[str, Any]) -> PlannedGet:
    return PlannedGet(
        identity=str(row["identity"]),
        provider=str(row["provider"]),
        endpoint=str(row["endpoint"]),
        day=str(row["day"]),
        params=dict(row.get("params") or {}),
    )


def _terminal_records(ledger: AppendOnlyLedger) -> dict[str, LedgerRecord]:
    terminals: dict[str, LedgerRecord] = {}
    skip = {
        ResultClassification.PENDING,
        ResultClassification.SKIPPED_ALREADY_COMPLETE,
    }
    for row in ledger.records():
        if row.result_classification in skip:
            continue
        terminals[row.canonical_request_identity] = row
    return terminals


def _city_date_from_parent(identity: str, cells: tuple[dict[str, Any], ...]) -> tuple[str, str]:
    if cells:
        return str(cells[0]["city"]), str(cells[0]["date"])
    if identity.startswith("clob:") and not identity.startswith("clob:range:"):
        rest = identity[len("clob:") :]
        if len(rest) >= 11 and rest[-11] == ":":
            return rest[:-11], rest[-10:]
    raise ValueError(f"cannot derive city/date from parent CLOB identity {identity}")


def _timezone_from_families(
    families: list[dict[str, Any]],
    *,
    city: str,
    day: str,
    stations: tuple[Station, ...],
) -> str:
    for row in families:
        if str(row.get("city")) == city and str(row.get("date")) == day:
            named = str(row.get("timezone_name") or "").strip()
            if named:
                return named
    for row in families:
        if str(row.get("city")) == city:
            named = str(row.get("timezone_name") or "").strip()
            if named:
                return named
    try:
        matched = stations_for_city(city, stations)
    except ValueError:
        return "UTC"
    return matched[0].timezone_name if matched else "UTC"


def _verify_existing_hashes(namespace: Path, ledger: AppendOnlyLedger) -> None:
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


def _write_recovery_progress(
    namespace: Path,
    authorized: AuthorizedRecoveryManifest,
    stage: CollectionStage,
    *,
    clock: datetime,
    terminal: bool = False,
    interrupt_reason: str | None = None,
) -> None:
    payload = {
        "collection_id": authorized.recovery_id,
        "interrupt_reason": interrupt_reason,
        "manifest_sha256": authorized.manifest_sha256,
        "parent_code_commit": str(authorized.payload.get("PARENT_CODE_COMMIT") or ""),
        "parent_collection_id": str(authorized.payload.get("PARENT_COLLECTION_ID") or ""),
        "parent_manifest_sha256": str(authorized.payload.get("PARENT_MANIFEST_SHA256") or ""),
        "recovery_code_commit": authorized.code_commit,
        "recovery_id": authorized.recovery_id,
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


def _copy_artifact(source: Path, destination: Path) -> None:
    if not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _load_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _load_list(path: Path) -> list[Any]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else []


def _load_object_list(path: Path) -> list[dict[str, Any]]:
    return [row for row in _load_list(path) if isinstance(row, dict)]


def _load_object_mapping(path: Path) -> dict[str, list[dict[str, Any]]]:
    payload = _load_object(path)
    out: dict[str, list[dict[str, Any]]] = {}
    for key, value in payload.items():
        if isinstance(value, list):
            out[str(key)] = [row for row in value if isinstance(row, dict)]
    return out
