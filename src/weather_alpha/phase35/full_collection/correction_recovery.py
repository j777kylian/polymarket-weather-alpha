"""Phase35B V2 CLOB correction-recovery path (offline planning + overlay).

Distinct from the legacy parent-derived 435 CLOB recovery planner. Targets are
derived only from the immutable first-recovery ledger plus the accepted V2
correction plan. Network execution requires a separate persisted manifest and
authorization receipt; no caller boolean may enable networking.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.models.timeutil import utc_now
from weather_alpha.phase35.full_collection.clob_contract import clob_range_params
from weather_alpha.phase35.full_collection.executor import PlannedGet
from weather_alpha.phase35.full_collection.manifest import (
    canonical_json,
    payload_sha256,
    resolve_code_commit,
)
from weather_alpha.phase35.full_collection.orchestrator import plan_to_dict
from weather_alpha.phase35.full_collection.policy import (
    CLOB_ATTEMPT_CAP,
    CLOB_ENDPOINT,
    CONCURRENCY,
    CORRECTION_AUTHORIZATION_SCHEMA_VERSION,
    CORRECTION_RAW_STORAGE_NAMESPACE,
    CORRECTION_REASON_MISSING_CANONICAL_FAMILY_OWNED_HISTORY,
    CORRECTION_SCHEMA_VERSION,
    CORRECTION_SCOPE_CLOB_V2,
    FIRST_RECOVERY_COLLECTION_ID,
    GLOBAL_GET_ATTEMPT_CAP,
    HASH_ALGORITHM,
    HTTP_METHOD,
    MAX_ATTEMPTS_PER_IDENTITY,
    MAX_RETRIES,
    PARSER_SCHEMA_VERSION,
    PRICE_PROVIDER,
    REQUEST_POLICY_VERSION,
    RETRY_AFTER_CAP_SECONDS,
    RETRY_MODE,
    RETRY_ONLY,
    RETRYABLE_HTTP_STATUSES,
    TIMEOUT_SECONDS,
    V2_CORRECTION_PROVENANCE_COUNT,
    V2_CORRECTION_TARGET_COUNT,
)
from weather_alpha.phase35.full_collection.provenance import (
    assert_text_has_no_machine_roots,
    atomic_write_json,
)
from weather_alpha.phase35.full_collection.v2_protocol import (
    CorrectionIdentityProvenance,
    CorrectionPlan,
    derive_correction_plan,
)

CORRECTION_AUTHORIZATION_REQUIRED_FIELDS: tuple[str, ...] = (
    "AUTHORIZED_AT",
    "CORRECTION_AUTHORIZATION_SCHEMA_VERSION",
    "CORRECTION_CODE_COMMIT",
    "CORRECTION_ID",
    "CORRECTION_IDENTITIES",
    "CORRECTION_MANIFEST_SHA256",
    "CORRECTION_NAMESPACE",
    "REQUEST_POLICY_VERSION",
    "V2_CORRECTION_AUDIT_SOURCE",
)


class CorrectionAuthorizationError(ValueError):
    """Fail-closed refusal for absent/invalid/mismatched correction authorization."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CorrectionProvenanceRow:
    identity: str
    event_family_id: str
    incorrect_old_identity: str | None
    incorrect_old_token: str | None
    correct_token: str
    start_ts: int
    end_ts: int
    fidelity: int
    ledger_evidence_collection_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class CorrectionRecoveryDerivation:
    identities: tuple[str, ...]
    provenance: tuple[CorrectionProvenanceRow, ...]
    correction_clob_identity_count: int
    correction_gamma_identity_count: int
    correction_ecmwf_identity_count: int
    first_recovery_collection_id: str
    plan: CorrectionPlan


@dataclass(frozen=True, slots=True)
class CorrectionManifestCreateResult:
    status: str
    written: bool
    correction_id: str | None
    manifest_sha256: str | None
    payload: dict[str, Any]
    identities: tuple[str, ...]
    derivation: CorrectionRecoveryDerivation

    def as_dict(self) -> dict[str, Any]:
        return {
            "CLOB_CORRECTION_IDENTITIES": len(self.identities),
            "ECMWF_CORRECTION_IDENTITIES": 0,
            "GAMMA_CORRECTION_IDENTITIES": 0,
            "PROVIDER_REQUESTS": 0,
            "correction_id": self.correction_id,
            "identities": list(self.identities),
            "manifest_sha256": self.manifest_sha256,
            "payload": self.payload,
            "status": self.status,
            "written": self.written,
        }


@dataclass(frozen=True, slots=True)
class CorrectionAuthorizationReceipt:
    correction_id: str
    correction_manifest_sha256: str
    correction_code_commit: str
    request_policy_version: str
    authorized_at: str
    schema_version: str
    correction_namespace: str
    correction_identities: tuple[str, ...]
    v2_correction_audit_source: str

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "AUTHORIZED_AT": self.authorized_at,
            "CORRECTION_AUTHORIZATION_SCHEMA_VERSION": self.schema_version,
            "CORRECTION_CODE_COMMIT": self.correction_code_commit,
            "CORRECTION_ID": self.correction_id,
            "CORRECTION_IDENTITIES": list(self.correction_identities),
            "CORRECTION_MANIFEST_SHA256": self.correction_manifest_sha256,
            "CORRECTION_NAMESPACE": self.correction_namespace,
            "REQUEST_POLICY_VERSION": self.request_policy_version,
            "V2_CORRECTION_AUDIT_SOURCE": self.v2_correction_audit_source,
        }
        assert_text_has_no_machine_roots(str(payload))
        return payload


@dataclass(frozen=True, slots=True)
class AuthorizedCorrectionManifest:
    path: Path
    payload: dict[str, Any]
    correction_id: str
    manifest_sha256: str
    code_commit: str
    request_policy_version: str
    network_authorized: bool
    correction_identities: tuple[str, ...]
    planned_gets: tuple[PlannedGet, ...]
    v2_correction_audit_source: str


def derive_v2_correction_targets(first_recovery_namespace: Path) -> CorrectionRecoveryDerivation:
    """Derive CLOB-only correction targets from the first-recovery ledger + V2 plan.

    Never uses the legacy parent-derived HTTP_FAILURE recovery planner.
    """

    families = _load_object_list(first_recovery_namespace / "events" / "accepted.json")
    clob_map_raw = _load_object(first_recovery_namespace / "plans" / "clob_cell_map.json")
    clob_cell_map = {
        str(key): value for key, value in clob_map_raw.items() if isinstance(value, list)
    }
    clob_parsed = _load_object_list(first_recovery_namespace / "parsed" / "clob.json")
    ledger_path = first_recovery_namespace / "ledger.jsonl"
    plan = derive_correction_plan(
        families=families,
        clob_cell_map=clob_cell_map,
        clob_parsed=clob_parsed,
        ledger_path=ledger_path if ledger_path.is_file() else None,
    )
    collection_id = _first_recovery_collection_id(first_recovery_namespace)
    if collection_id == FIRST_RECOVERY_COLLECTION_ID:
        if plan.correction_clob_identity_count != V2_CORRECTION_TARGET_COUNT:
            raise ValueError(
                f"expected {V2_CORRECTION_TARGET_COUNT} V2 correction identities; "
                f"got {plan.correction_clob_identity_count}"
            )
        if len(plan.provenance) != V2_CORRECTION_PROVENANCE_COUNT:
            raise ValueError(
                f"expected {V2_CORRECTION_PROVENANCE_COUNT} V2 correction provenance rows; "
                f"got {len(plan.provenance)}"
            )
    if plan.correction_gamma_identity_count != 0 or plan.correction_ecmwf_identity_count != 0:
        raise ValueError("V2 correction path allows CLOB identities only (Gamma=ECMWF=0)")
    provenance = tuple(_enrich_provenance(row) for row in plan.provenance)
    return CorrectionRecoveryDerivation(
        identities=plan.unresolved_correction_clob_identities,
        provenance=provenance,
        correction_clob_identity_count=plan.correction_clob_identity_count,
        correction_gamma_identity_count=0,
        correction_ecmwf_identity_count=0,
        first_recovery_collection_id=collection_id,
        plan=plan,
    )


def create_correction_recovery_manifest(
    *,
    destination: Path,
    first_recovery_namespace: Path,
    code_commit: str | None = None,
    created_at: datetime | None = None,
) -> CorrectionManifestCreateResult:
    created = created_at or utc_now()
    commit = resolve_code_commit(code_commit)
    derivation = derive_v2_correction_targets(first_recovery_namespace)
    planned: list[PlannedGet] = []
    cell_map: dict[str, list[dict[str, Any]]] = {}
    entries: list[dict[str, Any]] = []
    for row in derivation.provenance:
        params = clob_range_params(row.correct_token, row.start_ts, row.end_ts, row.fidelity)
        planned.append(
            PlannedGet(
                identity=row.identity,
                provider=PRICE_PROVIDER,
                endpoint=CLOB_ENDPOINT,
                day=_day_from_family(first_recovery_namespace, row.event_family_id),
                params=params,
            )
        )
        cell_map[row.identity] = _cells_for_family(
            first_recovery_namespace,
            event_family_id=row.event_family_id,
            fallback_old_identity=row.incorrect_old_identity,
        )
        entries.append(
            {
                "corrected_canonical_token": row.correct_token,
                "corrected_identity": row.identity,
                "correction_reason": row.reason,
                "end_ts": row.end_ts,
                "event_family_id": row.event_family_id,
                "fidelity": row.fidelity,
                "incorrect_old_identity": row.incorrect_old_identity,
                "incorrect_old_token": row.incorrect_old_token,
                "ledger_evidence_collection_id": row.ledger_evidence_collection_id,
                "original_requested_identity": row.incorrect_old_identity,
                "provenance_source": derivation.first_recovery_collection_id,
                "start_ts": row.start_ts,
            }
        )
    correction_id = _derive_correction_id(
        first_recovery_collection_id=derivation.first_recovery_collection_id,
        identities=derivation.identities,
        code_commit=commit,
        created_at=created,
    )
    payload: dict[str, Any] = {
        "CELL_MAP": cell_map,
        "CLOB_CORRECTION_IDENTITIES": len(planned),
        "CLOB_ENDPOINT": CLOB_ENDPOINT,
        "CLOB_REQUEST_POLICY_VERSION": REQUEST_POLICY_VERSION,
        "CORRECTION_CODE_COMMIT": commit,
        "CORRECTION_ENTRIES": entries,
        "CORRECTION_ID": correction_id,
        "CORRECTION_SCHEMA_VERSION": CORRECTION_SCHEMA_VERSION,
        "CORRECTION_SCOPE": CORRECTION_SCOPE_CLOB_V2,
        "CREATED_AT": created.isoformat(),
        "ECMWF_CORRECTION_IDENTITIES": 0,
        "FIRST_RECOVERY_COLLECTION_ID": derivation.first_recovery_collection_id,
        "GAMMA_CORRECTION_IDENTITIES": 0,
        "HASH_ALGORITHM": HASH_ALGORITHM,
        "PARSER_SCHEMA_VERSION": PARSER_SCHEMA_VERSION,
        "PLANNED_GETS": [plan_to_dict(row) for row in planned],
        "PRICE_PROVIDER": PRICE_PROVIDER,
        "RAW_STORAGE_NAMESPACE": CORRECTION_RAW_STORAGE_NAMESPACE,
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
        "V2_CORRECTION_AUDIT_SOURCE": derivation.first_recovery_collection_id,
        "price_semantics": "DESCRIPTIVE_ONLY",
    }
    encoded = canonical_json(payload)
    assert_text_has_no_machine_roots(encoded)
    if destination.is_file():
        raise ValueError(f"immutable correction manifest already exists: {destination.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(destination, payload)
    return CorrectionManifestCreateResult(
        status="OK",
        written=True,
        correction_id=correction_id,
        manifest_sha256=payload_sha256(payload),
        payload=payload,
        identities=derivation.identities,
        derivation=derivation,
    )


def inspect_correction_manifest(path: Path, *, expected_code_commit: str) -> dict[str, Any]:
    if not path.is_file():
        raise CorrectionAuthorizationError("missing", f"correction manifest not found: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CorrectionAuthorizationError(
            "invalid", "correction manifest is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise CorrectionAuthorizationError("invalid", "correction manifest must be a JSON object")
    if str(payload.get("CORRECTION_SCHEMA_VERSION") or "") != CORRECTION_SCHEMA_VERSION:
        raise CorrectionAuthorizationError(
            "invalid", "correction schema is not the frozen contract"
        )
    if str(payload.get("CORRECTION_SCOPE") or "") != CORRECTION_SCOPE_CLOB_V2:
        raise CorrectionAuthorizationError(
            "scope_mismatch", "correction scope is not CLOB_CORRECTION_V2"
        )
    gamma_ids = payload.get("GAMMA_CORRECTION_IDENTITIES")
    if gamma_ids is None or int(gamma_ids) != 0:
        raise CorrectionAuthorizationError(
            "scope_mismatch", "GAMMA correction identities must be 0"
        )
    ecmwf_ids = payload.get("ECMWF_CORRECTION_IDENTITIES")
    if ecmwf_ids is None or int(ecmwf_ids) != 0:
        raise CorrectionAuthorizationError(
            "scope_mismatch", "ECMWF correction identities must be 0"
        )
    if str(payload.get("CORRECTION_CODE_COMMIT") or "") != expected_code_commit:
        raise CorrectionAuthorizationError(
            "code_mismatch", "immutable CORRECTION_CODE_COMMIT does not match expected commit"
        )
    policy = payload.get("REQUEST_POLICY")
    if not isinstance(policy, dict) or str(policy.get("version")) != REQUEST_POLICY_VERSION:
        raise CorrectionAuthorizationError(
            "policy_mismatch", "REQUEST_POLICY version is not the frozen v2 contract"
        )
    planned = payload.get("PLANNED_GETS")
    if not isinstance(planned, list):
        raise CorrectionAuthorizationError("invalid", "PLANNED_GETS missing")
    if int(payload.get("CLOB_CORRECTION_IDENTITIES") or -1) != len(planned):
        raise CorrectionAuthorizationError(
            "invalid", "CLOB_CORRECTION_IDENTITIES does not match plan"
        )
    return payload


def create_correction_authorization_receipt(
    *,
    manifest_path: Path,
    destination: Path,
    expected_code_commit: str,
    authorized_at: datetime | None = None,
) -> CorrectionAuthorizationReceipt:
    payload = inspect_correction_manifest(manifest_path, expected_code_commit=expected_code_commit)
    if destination.resolve() == manifest_path.resolve():
        raise ValueError(
            "authorization receipt destination must not overwrite the correction manifest"
        )
    if destination.is_file():
        raise ValueError(
            f"immutable correction authorization receipt already exists: {destination.name}"
        )
    correction_id = str(payload["CORRECTION_ID"])
    identities = tuple(
        str(row["identity"])
        for row in payload["PLANNED_GETS"]
        if isinstance(row, dict) and row.get("identity")
    )
    receipt = CorrectionAuthorizationReceipt(
        correction_id=correction_id,
        correction_manifest_sha256=payload_sha256(payload),
        correction_code_commit=str(payload["CORRECTION_CODE_COMMIT"]),
        request_policy_version=REQUEST_POLICY_VERSION,
        authorized_at=(authorized_at or utc_now()).isoformat(),
        schema_version=CORRECTION_AUTHORIZATION_SCHEMA_VERSION,
        correction_namespace=_deterministic_correction_namespace(correction_id),
        correction_identities=identities,
        v2_correction_audit_source=str(payload["V2_CORRECTION_AUDIT_SOURCE"]),
    )
    atomic_write_json(destination, receipt.as_dict())
    return receipt


def load_authorized_correction_manifest(
    path: Path,
    *,
    expected_code_commit: str,
    authorization_path: Path,
) -> AuthorizedCorrectionManifest:
    payload = inspect_correction_manifest(path, expected_code_commit=expected_code_commit)
    receipt = _load_correction_receipt(authorization_path)
    digest = payload_sha256(payload)
    if receipt.correction_id != str(payload["CORRECTION_ID"]):
        raise CorrectionAuthorizationError(
            "collection_id_mismatch",
            "authorization receipt CORRECTION_ID does not match the correction manifest",
        )
    if receipt.correction_manifest_sha256 != digest:
        raise CorrectionAuthorizationError(
            "manifest_sha_mismatch",
            "authorization receipt CORRECTION_MANIFEST_SHA256 does not match the recomputed digest",
        )
    if receipt.correction_code_commit != str(payload["CORRECTION_CODE_COMMIT"]):
        raise CorrectionAuthorizationError(
            "code_mismatch",
            "authorization receipt CORRECTION_CODE_COMMIT does not match the correction manifest",
        )
    if receipt.request_policy_version != REQUEST_POLICY_VERSION:
        raise CorrectionAuthorizationError(
            "policy_mismatch",
            "authorization receipt REQUEST_POLICY_VERSION does not match request-policy-v2",
        )
    if receipt.v2_correction_audit_source != str(payload["V2_CORRECTION_AUDIT_SOURCE"]):
        raise CorrectionAuthorizationError(
            "audit_source_mismatch",
            "authorization receipt V2_CORRECTION_AUDIT_SOURCE does not match the manifest",
        )
    planned_ids = tuple(
        str(row["identity"])
        for row in payload["PLANNED_GETS"]
        if isinstance(row, dict) and row.get("identity")
    )
    if tuple(receipt.correction_identities) != planned_ids:
        raise CorrectionAuthorizationError(
            "identity_mismatch",
            "authorization receipt CORRECTION_IDENTITIES do not match the correction manifest",
        )
    expected_namespace = _deterministic_correction_namespace(str(payload["CORRECTION_ID"]))
    if receipt.correction_namespace != expected_namespace:
        raise CorrectionAuthorizationError(
            "namespace_mismatch",
            "authorization receipt CORRECTION_NAMESPACE does not match the deterministic "
            "namespace derived from verified correction manifest content",
        )
    planned = tuple(
        _planned_from_dict(row) for row in payload["PLANNED_GETS"] if isinstance(row, dict)
    )
    return AuthorizedCorrectionManifest(
        path=path,
        payload=payload,
        correction_id=str(payload["CORRECTION_ID"]),
        manifest_sha256=digest,
        code_commit=str(payload["CORRECTION_CODE_COMMIT"]),
        request_policy_version=REQUEST_POLICY_VERSION,
        network_authorized=True,
        correction_identities=planned_ids,
        planned_gets=planned,
        v2_correction_audit_source=str(payload["V2_CORRECTION_AUDIT_SOURCE"]),
    )


class CorrectionOverlayService:
    """Preserve first-recovery corpus; expose corrected CLOB only via overlay view."""

    def __init__(
        self,
        *,
        first_recovery_namespace: Path,
        correction_namespace: Path,
    ) -> None:
        self.first_recovery_namespace = first_recovery_namespace
        self.correction_namespace = correction_namespace

    def materialize_corrected_audit_view(self, *, destination: Path) -> Path:
        """Write a derived audit view without mutating the first-recovery corpus."""

        if destination.resolve() == self.first_recovery_namespace.resolve():
            raise ValueError("corrected audit view must not overwrite the first-recovery namespace")
        if destination.exists():
            raise ValueError(f"corrected audit view destination already exists: {destination}")
        destination.mkdir(parents=True)
        _copy_tree_file(
            self.first_recovery_namespace / "events" / "accepted.json",
            destination / "events" / "accepted.json",
        )
        quarantined = self.first_recovery_namespace / "events" / "quarantined.json"
        if quarantined.is_file():
            _copy_tree_file(quarantined, destination / "events" / "quarantined.json")
        _copy_tree_file(
            self.first_recovery_namespace / "expected_cells.json",
            destination / "expected_cells.json",
        )

        base_map = _load_object(self.first_recovery_namespace / "plans" / "clob_cell_map.json")
        overlay_map = _load_object(self.correction_namespace / "plans" / "clob_cell_map.json")
        base_parsed = _load_object_list(self.first_recovery_namespace / "parsed" / "clob.json")
        overlay_parsed = _load_object_list(self.correction_namespace / "parsed" / "clob.json")

        overlay_identities = {
            str(row.get("identity") or "") for row in overlay_parsed if row.get("identity")
        }
        overlay_families: set[str] = set()
        for cells in overlay_map.values():
            if not isinstance(cells, list):
                continue
            for cell in cells:
                if isinstance(cell, dict) and cell.get("event_family_id"):
                    overlay_families.add(str(cell["event_family_id"]))

        merged_map: dict[str, Any] = {}
        for identity, cells in base_map.items():
            if not isinstance(cells, list):
                continue
            kept = [
                cell
                for cell in cells
                if isinstance(cell, dict)
                and str(cell.get("event_family_id") or "") not in overlay_families
            ]
            if kept:
                merged_map[str(identity)] = kept
        for identity, cells in overlay_map.items():
            if isinstance(cells, list):
                merged_map[str(identity)] = [dict(cell) for cell in cells if isinstance(cell, dict)]

        merged_parsed: list[dict[str, Any]] = []
        for row in base_parsed:
            identity = str(row.get("identity") or "")
            if identity and identity in overlay_identities:
                continue
            families_for_row = {
                str(cell.get("event_family_id") or "")
                for cell in (base_map.get(identity) or [])
                if isinstance(cell, dict)
            }
            if families_for_row and families_for_row <= overlay_families:
                continue
            merged_parsed.append(dict(row))
        for row in overlay_parsed:
            merged_parsed.append(dict(row))

        _persist_json(destination / "plans" / "clob_cell_map.json", merged_map)
        _persist_json(destination / "parsed" / "clob.json", merged_parsed)
        _persist_json(
            destination / "correction_overlay" / "wrong_evidence_preserved.json",
            {
                "first_recovery_namespace": self.first_recovery_namespace.name,
                "overlay_identities": sorted(overlay_identities),
                "note": (
                    "Original first-recovery corpus is immutable; wrong CLOB evidence remains "
                    "under the first-recovery path. Corrected CLOB data lives only in the overlay."
                ),
            },
        )

        merged_ledger = destination / "ledger.jsonl"
        chunks: list[str] = []
        first_ledger = self.first_recovery_namespace / "ledger.jsonl"
        if first_ledger.is_file():
            chunks.append(first_ledger.read_text(encoding="utf-8").rstrip("\n"))
        overlay_ledger = self.correction_namespace / "ledger.jsonl"
        if overlay_ledger.is_file():
            chunks.append(overlay_ledger.read_text(encoding="utf-8").rstrip("\n"))
        merged_ledger.write_text(
            "\n".join(part for part in chunks if part) + "\n", encoding="utf-8"
        )

        _persist_json(
            destination / "correction_overlay" / "original_parsed_clob_snapshot.json",
            base_parsed,
        )
        return destination


class CorrectionRecoveryService:
    """Execute one authorized V2 CLOB correction recovery. GET-only; receipt required."""

    def __init__(
        self,
        *,
        manifest_path: Path,
        authorization_path: Path,
        correction_root: Path,
        first_recovery_namespace: Path,
        http: ReadOnlyHttpClient,
        expected_code_commit: str,
    ) -> None:
        self.manifest_path = manifest_path
        self.authorization_path = authorization_path
        self._correction_root = correction_root
        self._first_recovery_namespace = first_recovery_namespace
        self._http = http
        self._expected_code_commit = expected_code_commit

    def run(self) -> AuthorizedCorrectionManifest:
        # Fail closed: networking is gated solely by persisted receipt binding.
        load_authorized_correction_manifest(
            self.manifest_path,
            authorization_path=self.authorization_path,
            expected_code_commit=self._expected_code_commit,
        )
        raise CorrectionAuthorizationError(
            "execution_not_authorized_in_this_pass",
            "correction recovery network execution requires separate explicit authorization",
        )


def _enrich_provenance(row: CorrectionIdentityProvenance) -> CorrectionProvenanceRow:
    return CorrectionProvenanceRow(
        identity=row.identity,
        event_family_id=row.event_family_id,
        incorrect_old_identity=row.incorrect_old_identity,
        incorrect_old_token=row.incorrect_old_token,
        correct_token=row.correct_token,
        start_ts=row.start_ts,
        end_ts=row.end_ts,
        fidelity=row.fidelity,
        ledger_evidence_collection_id=row.ledger_evidence_collection_id,
        reason=CORRECTION_REASON_MISSING_CANONICAL_FAMILY_OWNED_HISTORY,
    )


def _first_recovery_collection_id(namespace: Path) -> str:
    progress = namespace / "progress.json"
    if progress.is_file():
        payload = _load_object(progress)
        for key in ("collection_id", "recovery_id"):
            value = payload.get(key)
            if value:
                return str(value)
    ledger = namespace / "ledger.jsonl"
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            collection_id = row.get("collection_id")
            if collection_id:
                return str(collection_id)
            break
    return namespace.name


def _derive_correction_id(
    *,
    first_recovery_collection_id: str,
    identities: tuple[str, ...],
    code_commit: str,
    created_at: datetime,
) -> str:
    material = {
        "code_commit": code_commit,
        "created_at": created_at.isoformat(),
        "first_recovery_collection_id": first_recovery_collection_id,
        "identities": list(identities),
        "scope": CORRECTION_SCOPE_CLOB_V2,
    }
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()[:12]
    return f"phase35-clob-correction-{digest}"


def _deterministic_correction_namespace(correction_id: str) -> str:
    """Canonical correction namespace from verified manifest CORRECTION_ID only."""

    return f"{CORRECTION_RAW_STORAGE_NAMESPACE}{correction_id}"


def _day_from_family(namespace: Path, event_family_id: str) -> str:
    families = _load_object_list(namespace / "events" / "accepted.json")
    for family in families:
        if str(family.get("event_family_id") or "") == event_family_id:
            return str(family.get("date") or "1970-01-01")
    return "1970-01-01"


def _cells_for_family(
    namespace: Path,
    *,
    event_family_id: str,
    fallback_old_identity: str | None,
) -> list[dict[str, Any]]:
    cell_map = _load_object(namespace / "plans" / "clob_cell_map.json")
    if fallback_old_identity and fallback_old_identity in cell_map:
        cells = cell_map[fallback_old_identity]
        if isinstance(cells, list):
            matched = [
                dict(cell)
                for cell in cells
                if isinstance(cell, dict)
                and str(cell.get("event_family_id") or "") == event_family_id
            ]
            return matched or [dict(cell) for cell in cells if isinstance(cell, dict)]
    out: list[dict[str, Any]] = []
    for cells in cell_map.values():
        if not isinstance(cells, list):
            continue
        for cell in cells:
            if isinstance(cell, dict) and str(cell.get("event_family_id") or "") == event_family_id:
                out.append(dict(cell))
    return out


def _load_correction_receipt(path: Path) -> CorrectionAuthorizationReceipt:
    if not path.is_file():
        raise CorrectionAuthorizationError(
            "missing_authorization", f"correction authorization receipt not found: {path.name}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CorrectionAuthorizationError(
            "invalid_authorization", "correction authorization receipt is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise CorrectionAuthorizationError(
            "invalid_authorization", "correction authorization receipt must be a JSON object"
        )
    missing = [key for key in CORRECTION_AUTHORIZATION_REQUIRED_FIELDS if key not in payload]
    if missing:
        raise CorrectionAuthorizationError(
            "invalid_authorization",
            "correction authorization receipt is missing required fields",
        )
    identities_raw = payload["CORRECTION_IDENTITIES"]
    if not isinstance(identities_raw, list):
        raise CorrectionAuthorizationError(
            "invalid_authorization", "CORRECTION_IDENTITIES must be a list"
        )
    return CorrectionAuthorizationReceipt(
        correction_id=str(payload["CORRECTION_ID"]),
        correction_manifest_sha256=str(payload["CORRECTION_MANIFEST_SHA256"]),
        correction_code_commit=str(payload["CORRECTION_CODE_COMMIT"]),
        request_policy_version=str(payload["REQUEST_POLICY_VERSION"]),
        authorized_at=str(payload["AUTHORIZED_AT"]),
        schema_version=str(payload["CORRECTION_AUTHORIZATION_SCHEMA_VERSION"]),
        correction_namespace=str(payload["CORRECTION_NAMESPACE"]),
        correction_identities=tuple(str(item) for item in identities_raw),
        v2_correction_audit_source=str(payload["V2_CORRECTION_AUDIT_SOURCE"]),
    )


def _planned_from_dict(row: dict[str, Any]) -> PlannedGet:
    raw_params = row.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}
    return PlannedGet(
        identity=str(row["identity"]),
        provider=str(row.get("provider") or PRICE_PROVIDER),
        endpoint=str(row.get("endpoint") or CLOB_ENDPOINT),
        day=str(row.get("day") or ""),
        params={str(key): value for key, value in params.items()},
    )


def _load_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _load_object_list(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _persist_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def _copy_tree_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
