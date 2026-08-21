"""Production dataset freeze from persisted collection artifacts.

Offline: consumes a completed collection namespace, recomputes audit, and binds
real hashes. Does not contact providers, create manifests, or authorize collection.

V2 freeze adapter (``build_production_v2_dataset_freeze``) binds corrected V2
audit + correction provenance for a *future* freeze path. It does not contact
providers and never enables caller readiness overrides.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from weather_alpha.phase35.full_collection.audit import (
    DatasetAuditResult,
    DatasetFreeze,
    DatasetObservation,
    ExpectedCell,
    audit_dataset,
    build_dataset_audit_reports,
    build_dataset_freeze,
)
from weather_alpha.phase35.full_collection.corpus import FullCollectionCorpusAssembler
from weather_alpha.phase35.full_collection.ledger import AppendOnlyLedger
from weather_alpha.phase35.full_collection.manifest import payload_sha256, resolve_code_commit
from weather_alpha.phase35.full_collection.policy import (
    CORRECTION_SCOPE_CLOB_V2,
    END_DATE,
    START_DATE,
)
from weather_alpha.phase35.full_collection.provenance import (
    assert_text_has_no_machine_roots,
    atomic_write_json,
    probe_raw,
)
from weather_alpha.phase35.full_collection.v2_protocol import offline_v2_corpus_audit

FREEZE_ARTIFACT_NAME = "phase35_dataset_freeze.json"
HISTORICAL_AUDIT_JSON = "phase35_historical_audit.json"
FREEZE_ARTIFACT_RELATIVE = f"reports/{FREEZE_ARTIFACT_NAME}"
AUDIT_ARTIFACT_RELATIVE = f"reports/{HISTORICAL_AUDIT_JSON}"
V2_FREEZE_ARTIFACT_NAME = "phase35_v2_dataset_freeze.json"
V2_AUDIT_JSON = "phase35_v2_audit.json"
V2_FREEZE_ARTIFACT_RELATIVE = f"reports/{V2_FREEZE_ARTIFACT_NAME}"
V2_AUDIT_ARTIFACT_RELATIVE = f"reports/{V2_AUDIT_JSON}"
PLACEHOLDER_HASHES = frozenset({"", "none", "uncollected", "0" * 64})
CORRECTED_AUDIT_VIEW_NAME = "corrected_audit_view"
WRONG_EVIDENCE_RELATIVE = "correction_overlay/wrong_evidence_preserved.json"


def is_phase35b_v2_production_freeze_namespace(namespace: Path) -> bool:
    """True when namespace is a Phase35B V2 correction freeze target."""

    return (namespace / "correction-manifest.json").is_file() and (
        namespace / "correction_lineage.json"
    ).is_file()


class DatasetFreezeStatus(StrEnum):
    SUCCESS = "SUCCESS"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class ProductionDatasetFreezeResult:
    status: DatasetFreezeStatus
    dataset_id: str
    dataset_freeze_created: bool
    reason: str | None
    manifest_sha256: str | None
    raw_index_sha256: str | None
    canonical_dataset_sha256: str | None
    audit_report_sha256: str | None
    freeze_sha256: str | None
    freeze_path: Path | None
    freeze: DatasetFreeze | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "AUDIT_ARTIFACT": AUDIT_ARTIFACT_RELATIVE,
            "AUDIT_REPORT_SHA256": self.audit_report_sha256,
            "CANONICAL_DATASET_SHA256": self.canonical_dataset_sha256,
            "DATASET_FREEZE_CREATED": "YES" if self.dataset_freeze_created else "NO",
            "DATASET_ID": self.dataset_id,
            "FREEZE_ARTIFACT": FREEZE_ARTIFACT_RELATIVE if self.dataset_freeze_created else None,
            "FREEZE_SHA256": self.freeze_sha256,
            "MANIFEST_SHA256": self.manifest_sha256,
            "PROVIDER_REQUESTS": 0,
            "RAW_INDEX_SHA256": self.raw_index_sha256,
            "reason": self.reason,
            "status": self.status.value,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        assert_text_has_no_machine_roots(encoded)
        return payload


def build_production_dataset_freeze(
    *,
    collection_root: Path,
    collection_id: str,
    manifest_path: Path | None = None,
    code_commit: str | None = None,
) -> ProductionDatasetFreezeResult:
    """Bind a production freeze from persisted collection/audit artifacts.

    AUDIT_REPORT_SHA256 hashes the canonical JSON serialization
    (sort_keys=True, ensure_ascii=True, compact separators) of the authoritative
    machine artifact ``reports/phase35_historical_audit.json``, not Markdown.
    """
    dataset_id = f"phase35-dataset-{collection_id}"
    namespace = collection_root / collection_id
    try:
        return _build_or_refuse(
            namespace=namespace,
            collection_root=collection_root,
            collection_id=collection_id,
            dataset_id=dataset_id,
            manifest_path=manifest_path,
            code_commit=code_commit,
        )
    except (
        FileNotFoundError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        OSError,
        KeyError,
    ):
        return _refused(dataset_id, "assemble_failed")


def _build_or_refuse(
    *,
    namespace: Path,
    collection_root: Path,
    collection_id: str,
    dataset_id: str,
    manifest_path: Path | None,
    code_commit: str | None,
) -> ProductionDatasetFreezeResult:
    progress = _load_progress(namespace)
    if progress is None:
        return _refused(dataset_id, "missing_progress")
    if str(progress.get("collection_id") or "") != collection_id:
        return _refused(dataset_id, "collection_id_mismatch")
    stage = str(progress.get("stage") or "")
    if stage != "COMPLETE" or progress.get("terminal") is not True:
        return _refused(dataset_id, "not_complete")
    manifest_sha256 = str(progress.get("manifest_sha256") or "")
    if not _is_real_sha256(manifest_sha256):
        return _refused(dataset_id, "missing_manifest_sha256")

    date_range = {"end": END_DATE, "start": START_DATE}
    resolved_commit = code_commit or resolve_code_commit()
    if manifest_path is not None:
        verified = _verify_optional_manifest(
            manifest_path, expected_sha256=manifest_sha256, dataset_id=dataset_id
        )
        if isinstance(verified, ProductionDatasetFreezeResult):
            return verified
        resolved_commit = str(
            verified.get("CODE_COMMIT") or verified.get("RECOVERY_CODE_COMMIT") or resolved_commit
        )
        date_range = {
            "end": str(verified.get("END_DATE") or END_DATE),
            "start": str(verified.get("START_DATE") or START_DATE),
        }

    try:
        corpus = FullCollectionCorpusAssembler(
            collection_root=collection_root,
            collection_id=collection_id,
        ).assemble()
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError):
        return _refused(dataset_id, "assemble_failed")

    audit = audit_dataset(expected=corpus.expected, observations=corpus.observations)
    if not audit.phase35_dataset_ready:
        return _refused(dataset_id, "dataset_not_ready")

    audit_path = namespace / "reports" / HISTORICAL_AUDIT_JSON
    if not audit_path.is_file():
        return _refused(dataset_id, "missing_audit_report")
    try:
        persisted_audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _refused(dataset_id, "missing_audit_report")
    if not isinstance(persisted_audit, dict):
        return _refused(dataset_id, "audit_mismatch")
    recomputed_machine = build_dataset_audit_reports(audit, collection_not_executed=False)[0]
    recomputed_machine = json.loads(
        json.dumps(recomputed_machine, sort_keys=True, ensure_ascii=True)
    )
    if _canonical_sha256(persisted_audit) != _canonical_sha256(recomputed_machine):
        return _refused(dataset_id, "audit_mismatch")
    # Canonical compact serialization of the authoritative machine JSON, not Markdown.
    audit_report_sha256 = _canonical_sha256(persisted_audit)

    ledger_path = namespace / "ledger.jsonl"
    if not ledger_path.is_file():
        return _refused(dataset_id, "raw_hash_mismatch")
    ledger = AppendOnlyLedger(ledger_path)
    if _raw_integrity_failed(namespace, ledger):
        return _refused(dataset_id, "raw_hash_mismatch")
    raw_index_sha256 = _raw_index_sha256(ledger)

    accepted = _load_object_list(namespace / "events" / "accepted.json")
    quarantine = list(corpus.quarantine)
    canonical_dataset_sha256 = _canonical_dataset_sha256(
        expected=corpus.expected,
        observations=corpus.observations,
        accepted=accepted,
        quarantine=quarantine,
    )
    hashes = (
        manifest_sha256,
        raw_index_sha256,
        canonical_dataset_sha256,
        audit_report_sha256,
    )
    if any(not _is_real_sha256(item) for item in hashes):
        return _refused(dataset_id, "placeholder_hash")

    freeze = build_dataset_freeze(
        audit,
        collection_id=collection_id,
        code_commit=resolved_commit,
        manifest_sha256=manifest_sha256,
        raw_index_sha256=raw_index_sha256,
        canonical_dataset_sha256=canonical_dataset_sha256,
        report_sha256=audit_report_sha256,
        date_range=date_range,
        event_count=len(accepted),
        snapshot_count=len(corpus.observations),
        checkpoint_counts=_dimension_counts(corpus.expected, "checkpoint"),
        city_counts=_dimension_counts(corpus.expected, "city"),
        missingness_summary=_missingness_summary(audit, corpus.expected, corpus.observations),
        quarantine_summary=_quarantine_summary(quarantine),
    )
    if freeze is None:
        return _refused(dataset_id, "dataset_not_ready")
    payload = freeze.as_dict()
    payload["MONTH_COUNTS"] = _dimension_counts(corpus.expected, "month")
    payload["STATION_COUNTS"] = _dimension_counts(corpus.expected, "station")
    lineage = _load_recovery_lineage(namespace)
    if lineage is not None:
        payload["RECOVERY_LINEAGE"] = lineage
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    assert_text_has_no_machine_roots(encoded)
    freeze = DatasetFreeze(payload=payload)
    destination = namespace / "reports" / FREEZE_ARTIFACT_NAME
    freeze_sha256 = atomic_write_json(destination, payload)
    return ProductionDatasetFreezeResult(
        status=DatasetFreezeStatus.SUCCESS,
        dataset_id=dataset_id,
        dataset_freeze_created=True,
        reason=None,
        manifest_sha256=manifest_sha256,
        raw_index_sha256=raw_index_sha256,
        canonical_dataset_sha256=canonical_dataset_sha256,
        audit_report_sha256=audit_report_sha256,
        freeze_sha256=freeze_sha256,
        freeze_path=destination,
        freeze=freeze,
    )


def _refused(dataset_id: str, reason: str) -> ProductionDatasetFreezeResult:
    return ProductionDatasetFreezeResult(
        status=DatasetFreezeStatus.REFUSED,
        dataset_id=dataset_id,
        dataset_freeze_created=False,
        reason=reason,
        manifest_sha256=None,
        raw_index_sha256=None,
        canonical_dataset_sha256=None,
        audit_report_sha256=None,
        freeze_sha256=None,
        freeze_path=None,
        freeze=None,
    )


def _load_progress(namespace: Path) -> dict[str, Any] | None:
    path = namespace / "progress.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _verify_optional_manifest(
    manifest_path: Path,
    *,
    expected_sha256: str,
    dataset_id: str,
) -> dict[str, Any] | ProductionDatasetFreezeResult:
    if not manifest_path.is_file():
        return _refused(dataset_id, "manifest_sha_mismatch")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return _refused(dataset_id, "manifest_sha_mismatch")
    if payload_sha256(payload) != expected_sha256:
        return _refused(dataset_id, "manifest_sha_mismatch")
    return payload


def _is_real_sha256(value: str) -> bool:
    if value in PLACEHOLDER_HASHES:
        return False
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _raw_integrity_failed(namespace: Path, ledger: AppendOnlyLedger) -> bool:
    for record in ledger.records():
        if not record.content_sha256 or not record.stable_raw_provenance_path:
            continue
        try:
            probe = probe_raw(
                namespace / Path(record.stable_raw_provenance_path),
                record.content_sha256,
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return True
        if probe.fail_closed:
            return True
    return False


def _raw_index_sha256(ledger: AppendOnlyLedger) -> str:
    rows = [
        {
            "attempt_number": record.attempt_number,
            "canonical_request_identity": record.canonical_request_identity,
            "content_sha256": record.content_sha256,
            "parser_schema_version": record.parser_schema_version,
            "provider": record.provider,
            "result_classification": record.result_classification.value,
            "stable_raw_provenance_path": record.stable_raw_provenance_path,
        }
        for record in ledger.records()
    ]
    rows.sort(key=_canonical_bytes)
    return _canonical_sha256(rows)


def _canonical_dataset_sha256(
    *,
    expected: tuple[ExpectedCell, ...],
    observations: tuple[DatasetObservation, ...],
    accepted: list[dict[str, Any]],
    quarantine: list[dict[str, Any]],
) -> str:
    payload = {
        "accepted_families": _sorted_records(accepted),
        "expected": _sorted_records([row.as_dict() for row in expected]),
        "observations": _sorted_records([row.as_dict() for row in observations]),
        "quarantine": _sorted_records(quarantine),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    assert_text_has_no_machine_roots(encoded)
    return _canonical_sha256(payload)


def _sorted_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=_canonical_bytes)


def _dimension_counts(cells: tuple[ExpectedCell, ...], attr: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for cell in cells:
        counts[str(getattr(cell, attr))] += 1
    return dict(sorted(counts.items()))


def _missingness_summary(
    audit: DatasetAuditResult,
    expected: tuple[ExpectedCell, ...],
    observations: tuple[DatasetObservation, ...],
) -> dict[str, Any]:
    return {
        "blocked_reasons": list(audit.blocked_reasons),
        "expected_count": len(expected),
        "future_leakage_count": audit.future_leakage_count,
        "observed_count": sum(1 for row in observations if row.observed),
        "raw_provenance_hash_failures": audit.raw_provenance_hash_failures,
        "retrospective_substitution_count": audit.retrospective_substitution_count,
        "usable_count": sum(1 for row in observations if row.usable),
    }


def _quarantine_summary(quarantine: list[dict[str, Any]]) -> dict[str, Any]:
    reasons: dict[str, int] = defaultdict(int)
    for row in quarantine:
        reasons[str(row.get("reason") or "unspecified")] += 1
    return {"count": len(quarantine), "reasons": dict(sorted(reasons.items()))}


def _load_object_list(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _load_recovery_lineage(namespace: Path) -> dict[str, Any] | None:
    path = namespace / "recovery_lineage.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    parent_id = str(payload.get("PARENT_COLLECTION_ID") or "")
    recovery_id = str(payload.get("RECOVERY_ID") or "")
    if not parent_id or not recovery_id:
        return None
    lineage = {
        "PARENT_CODE_COMMIT": str(payload.get("PARENT_CODE_COMMIT") or ""),
        "PARENT_COLLECTION_ID": parent_id,
        "PARENT_MANIFEST_SHA256": str(payload.get("PARENT_MANIFEST_SHA256") or ""),
        "RECOVERY_CODE_COMMIT": str(payload.get("RECOVERY_CODE_COMMIT") or ""),
        "RECOVERY_ID": recovery_id,
        "RECOVERY_MANIFEST_SHA256": str(payload.get("RECOVERY_MANIFEST_SHA256") or ""),
        "RECOVERY_SCOPE": str(payload.get("RECOVERY_SCOPE") or ""),
    }
    encoded = json.dumps(lineage, sort_keys=True, ensure_ascii=True)
    assert_text_has_no_machine_roots(encoded)
    return lineage


@dataclass(frozen=True, slots=True)
class ProductionV2DatasetFreezeResult:
    status: DatasetFreezeStatus
    correction_id: str
    dataset_freeze_created: bool
    reason: str | None
    freeze_sha256: str | None
    freeze_path: Path | None
    payload: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        summary = {
            "CORRECTION_ID": self.correction_id,
            "DATASET_FREEZE_CREATED": "YES" if self.dataset_freeze_created else "NO",
            "FREEZE_ARTIFACT": V2_FREEZE_ARTIFACT_RELATIVE if self.dataset_freeze_created else None,
            "FREEZE_SHA256": self.freeze_sha256,
            "PROVIDER_REQUESTS": 0,
            "reason": self.reason,
            "status": self.status.value,
        }
        encoded = json.dumps(summary, sort_keys=True, ensure_ascii=True)
        assert_text_has_no_machine_roots(encoded)
        return summary


def build_production_v2_dataset_freeze(
    *,
    correction_namespace: Path,
) -> ProductionV2DatasetFreezeResult:
    """Bind a V2 production freeze from persisted correction + derived audit view.

    Offline only. The corrected audit view path is derived exclusively as
    ``<correction_namespace>/corrected_audit_view`` — callers cannot supply an
    alternate view. Writes ``reports/phase35_v2_dataset_freeze.json`` only when
    every generic integrity gate and V2 freeze-eligibility gate passes. Caller
    cannot override readiness. Does not contact providers.
    """
    correction_id = correction_namespace.name
    corrected_audit_view = correction_namespace / CORRECTED_AUDIT_VIEW_NAME
    try:
        return _build_v2_or_refuse(
            correction_namespace=correction_namespace,
            corrected_audit_view=corrected_audit_view,
            correction_id=correction_id,
        )
    except (
        FileNotFoundError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        OSError,
        KeyError,
    ):
        return _v2_refused(correction_id, "assemble_failed")


def _v2_refused(correction_id: str, reason: str) -> ProductionV2DatasetFreezeResult:
    return ProductionV2DatasetFreezeResult(
        status=DatasetFreezeStatus.REFUSED,
        correction_id=correction_id,
        dataset_freeze_created=False,
        reason=reason,
        freeze_sha256=None,
        freeze_path=None,
        payload=None,
    )


def _build_v2_or_refuse(
    *,
    correction_namespace: Path,
    corrected_audit_view: Path,
    correction_id: str,
) -> ProductionV2DatasetFreezeResult:
    progress = _load_progress(correction_namespace)
    if progress is None:
        return _v2_refused(correction_id, "missing_progress")
    if str(progress.get("correction_id") or progress.get("collection_id") or "") != correction_id:
        return _v2_refused(correction_id, "correction_id_mismatch")
    stage = str(progress.get("stage") or "")
    if stage != "COMPLETE" or progress.get("terminal") is not True:
        return _v2_refused(correction_id, "not_complete")

    manifest_path = correction_namespace / "correction-manifest.json"
    receipt_path = correction_namespace / "correction-authorization.json"
    lineage_path = correction_namespace / "correction_lineage.json"
    if not manifest_path.is_file() or not receipt_path.is_file() or not lineage_path.is_file():
        return _v2_refused(correction_id, "missing_provenance")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _v2_refused(correction_id, "missing_provenance")
    if (
        not isinstance(manifest, dict)
        or not isinstance(receipt, dict)
        or not isinstance(lineage, dict)
    ):
        return _v2_refused(correction_id, "missing_provenance")

    manifest_sha256 = payload_sha256(manifest)
    receipt_sha256 = payload_sha256(receipt)
    lineage_sha256 = payload_sha256(lineage)
    progress_manifest_sha = str(progress.get("manifest_sha256") or "")
    if progress_manifest_sha != manifest_sha256:
        return _v2_refused(correction_id, "manifest_sha_mismatch")
    if str(receipt.get("CORRECTION_MANIFEST_SHA256") or "") != manifest_sha256:
        return _v2_refused(correction_id, "provenance_mismatch")
    if str(lineage.get("CORRECTION_MANIFEST_SHA256") or "") != manifest_sha256:
        return _v2_refused(correction_id, "lineage_mismatch")

    expected_id = str(manifest.get("CORRECTION_ID") or "")
    if expected_id != correction_id:
        return _v2_refused(correction_id, "correction_id_mismatch")
    if str(receipt.get("CORRECTION_ID") or "") != correction_id:
        return _v2_refused(correction_id, "provenance_mismatch")
    if str(lineage.get("CORRECTION_ID") or "") != correction_id:
        return _v2_refused(correction_id, "lineage_mismatch")

    code_commit = str(manifest.get("CORRECTION_CODE_COMMIT") or "")
    if not code_commit or code_commit != str(receipt.get("CORRECTION_CODE_COMMIT") or ""):
        return _v2_refused(correction_id, "provenance_mismatch")
    if code_commit != str(lineage.get("CORRECTION_CODE_COMMIT") or ""):
        return _v2_refused(correction_id, "lineage_mismatch")
    if code_commit != str(progress.get("correction_code_commit") or ""):
        return _v2_refused(correction_id, "provenance_mismatch")

    relative_namespace = str(lineage.get("CORRECTION_NAMESPACE") or "")
    if not relative_namespace or Path(relative_namespace).is_absolute():
        return _v2_refused(correction_id, "namespace_mismatch")
    if relative_namespace != str(receipt.get("CORRECTION_NAMESPACE") or ""):
        return _v2_refused(correction_id, "provenance_mismatch")
    if "CORRECTION_NAMESPACE" in manifest and str(manifest["CORRECTION_NAMESPACE"]) != (
        relative_namespace
    ):
        return _v2_refused(correction_id, "namespace_mismatch")
    if relative_namespace != str(progress.get("correction_namespace") or ""):
        return _v2_refused(correction_id, "namespace_mismatch")

    scope = str(lineage.get("CORRECTION_SCOPE") or "")
    if scope != CORRECTION_SCOPE_CLOB_V2:
        return _v2_refused(correction_id, "scope_mismatch")
    if str(manifest.get("CORRECTION_SCOPE") or "") != CORRECTION_SCOPE_CLOB_V2:
        return _v2_refused(correction_id, "scope_mismatch")

    audit_source = str(lineage.get("V2_CORRECTION_AUDIT_SOURCE") or "")
    first_recovery_id = str(lineage.get("FIRST_RECOVERY_COLLECTION_ID") or "")
    if not audit_source or not first_recovery_id:
        return _v2_refused(correction_id, "lineage_mismatch")
    if audit_source != str(receipt.get("V2_CORRECTION_AUDIT_SOURCE") or ""):
        return _v2_refused(correction_id, "provenance_mismatch")
    if audit_source != str(manifest.get("V2_CORRECTION_AUDIT_SOURCE") or ""):
        return _v2_refused(correction_id, "provenance_mismatch")
    if first_recovery_id != str(progress.get("first_recovery_collection_id") or ""):
        return _v2_refused(correction_id, "lineage_mismatch")

    if not corrected_audit_view.is_dir():
        return _v2_refused(correction_id, "missing_corrected_view")
    wrong_evidence = corrected_audit_view / "correction_overlay" / "wrong_evidence_preserved.json"
    original_snapshot = (
        corrected_audit_view / "correction_overlay" / "original_parsed_clob_snapshot.json"
    )
    if not wrong_evidence.is_file() or not original_snapshot.is_file():
        return _v2_refused(correction_id, "overlay_validation_failed")
    try:
        wrong_payload = json.loads(wrong_evidence.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _v2_refused(correction_id, "overlay_validation_failed")
    if not isinstance(wrong_payload, dict) or not wrong_payload.get("overlay_identities"):
        return _v2_refused(correction_id, "overlay_validation_failed")

    preservation_evidence_hash = _file_sha256_path(wrong_evidence)
    overlay_canonical_hash = _corrected_overlay_canonical_hash(corrected_audit_view)
    overlay_view_index_sha256 = _corrected_overlay_view_index_sha256(corrected_audit_view)
    ledger_path = correction_namespace / "ledger.jsonl"
    raw_overlay_index_hash = _raw_overlay_index_hash(correction_namespace)
    if any(
        not _is_real_sha256(item)
        for item in (
            preservation_evidence_hash,
            overlay_canonical_hash,
            overlay_view_index_sha256,
            raw_overlay_index_hash,
        )
    ):
        return _v2_refused(correction_id, "placeholder_hash")

    audit_path = correction_namespace / "reports" / V2_AUDIT_JSON
    if not audit_path.is_file():
        return _v2_refused(correction_id, "missing_v2_audit")
    try:
        persisted_audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _v2_refused(correction_id, "missing_v2_audit")
    if not isinstance(persisted_audit, dict):
        return _v2_refused(correction_id, "v2_audit_mismatch")

    # Authoritative V2 audit only: no readiness reinterpretation.
    recomputed = offline_v2_corpus_audit(corrected_audit_view)
    if _canonical_sha256(persisted_audit) != _canonical_sha256(recomputed):
        return _v2_refused(correction_id, "v2_audit_mismatch")

    unresolved = int(recomputed.get("UNRESOLVED_CORRECTION_CLOB_IDENTITY_COUNT") or 0)
    ownership = int(recomputed.get("TOKEN_OWNERSHIP_VIOLATION_COUNT") or 0)
    future = int(
        recomputed.get("ACTUAL_SELECTED_FUTURE_PRICE_COUNT")
        or recomputed.get("ACTUAL_FUTURE_LEAKAGE_COUNT")
        or 0
    )
    if unresolved != 0 or ownership != 0 or future != 0:
        return _v2_refused(correction_id, "v2_gates_failed")

    if str(recomputed.get("PHASE35B_V2_DATASET_READY") or "") != "YES":
        return _v2_refused(correction_id, "v2_dataset_not_ready")

    try:
        corpus = FullCollectionCorpusAssembler(
            collection_root=corrected_audit_view.parent,
            collection_id=corrected_audit_view.name,
        ).assemble()
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError):
        return _v2_refused(correction_id, "generic_not_ready")
    generic_audit = audit_dataset(expected=corpus.expected, observations=corpus.observations)
    if not generic_audit.phase35_dataset_ready:
        return _v2_refused(correction_id, "generic_not_ready")

    audit_canonical_sha256 = _canonical_sha256(persisted_audit)
    audit_file_sha256 = _file_sha256_path(audit_path)
    manifest_file_sha256 = _file_sha256_path(manifest_path)
    receipt_file_sha256 = _file_sha256_path(receipt_path)
    lineage_file_sha256 = _file_sha256_path(lineage_path)
    raw_overlay_index_file_sha256 = _file_sha256_path(ledger_path)
    wrong_evidence_file_sha256 = preservation_evidence_hash

    corrected_audit_relative = f"{relative_namespace}/{V2_AUDIT_ARTIFACT_RELATIVE}"
    overlay_relative = f"{relative_namespace}/{CORRECTED_AUDIT_VIEW_NAME}"
    wrong_relative = f"{overlay_relative}/{WRONG_EVIDENCE_RELATIVE}"
    manifest_relative = f"{relative_namespace}/correction-manifest.json"
    receipt_relative = f"{relative_namespace}/correction-authorization.json"
    lineage_relative = f"{relative_namespace}/correction_lineage.json"
    raw_index_relative = f"{relative_namespace}/ledger.jsonl"

    payload: dict[str, Any] = {
        "CORRECTED_AUDIT_CANONICAL_SHA256": audit_canonical_sha256,
        "CORRECTED_AUDIT_FILE_SHA256": audit_file_sha256,
        "CORRECTED_AUDIT_IDENTITY": "phase35_v2_audit",
        "CORRECTED_AUDIT_RELATIVE_PATH": corrected_audit_relative,
        "CORRECTED_OVERLAY_CANONICAL_HASH": overlay_canonical_hash,
        "CORRECTED_OVERLAY_RELATIVE_PATH": overlay_relative,
        "CORRECTED_OVERLAY_VIEW_IDENTITY": CORRECTED_AUDIT_VIEW_NAME,
        "CORRECTED_OVERLAY_VIEW_INDEX_SHA256": overlay_view_index_sha256,
        "CORRECTED_OVERLAY_VIEW_RELATIVE_PATH": overlay_relative,
        "CORRECTION_CODE_COMMIT": code_commit,
        "CORRECTION_ID": correction_id,
        "CORRECTION_LINEAGE_FILE_SHA256": lineage_file_sha256,
        "CORRECTION_LINEAGE_RELATIVE_PATH": lineage_relative,
        "CORRECTION_LINEAGE_SHA256": lineage_sha256,
        "CORRECTION_MANIFEST_FILE_SHA256": manifest_file_sha256,
        "CORRECTION_MANIFEST_RELATIVE_PATH": manifest_relative,
        "CORRECTION_MANIFEST_SHA256": manifest_sha256,
        "CORRECTION_NAMESPACE": relative_namespace,
        "CORRECTION_RECEIPT_FILE_SHA256": receipt_file_sha256,
        "CORRECTION_RECEIPT_RELATIVE_PATH": receipt_relative,
        "CORRECTION_RECEIPT_SHA256": receipt_sha256,
        "CORRECTION_SCOPE": scope,
        "FIRST_RECOVERY_COLLECTION_ID": first_recovery_id,
        "PRESERVATION_EVIDENCE_HASH": preservation_evidence_hash,
        "PROVIDER_REQUESTS": 0,
        "RAW_OVERLAY_INDEX_FILE_SHA256": raw_overlay_index_file_sha256,
        "RAW_OVERLAY_INDEX_HASH": raw_overlay_index_hash,
        "RAW_OVERLAY_INDEX_RELATIVE_PATH": raw_index_relative,
        "V2_CORRECTION_AUDIT_SOURCE": audit_source,
        "V2_FREEZE_ARTIFACT": V2_FREEZE_ARTIFACT_RELATIVE,
        "WRONG_EVIDENCE_PRESERVED_FILE_SHA256": wrong_evidence_file_sha256,
        "WRONG_EVIDENCE_PRESERVED_RELATIVE_PATH": wrong_relative,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    assert_text_has_no_machine_roots(encoded)
    for value in (
        relative_namespace,
        corrected_audit_relative,
        overlay_relative,
        wrong_relative,
        manifest_relative,
        receipt_relative,
        lineage_relative,
        raw_index_relative,
    ):
        if Path(value).is_absolute():
            return _v2_refused(correction_id, "namespace_mismatch")

    destination = correction_namespace / "reports" / V2_FREEZE_ARTIFACT_NAME
    freeze_sha256 = atomic_write_json(destination, payload)
    return ProductionV2DatasetFreezeResult(
        status=DatasetFreezeStatus.SUCCESS,
        correction_id=correction_id,
        dataset_freeze_created=True,
        reason=None,
        freeze_sha256=freeze_sha256,
        freeze_path=destination,
        payload=payload,
    )


def _file_sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _corrected_overlay_canonical_hash(view: Path) -> str:
    material = {
        "events_accepted": _load_json_any(view / "events" / "accepted.json"),
        "expected_cells": _load_json_any(view / "expected_cells.json"),
        "parsed_clob": _load_json_any(view / "parsed" / "clob.json"),
        "plans_clob_cell_map": _load_json_any(view / "plans" / "clob_cell_map.json"),
        "wrong_evidence_preserved": _load_json_any(
            view / "correction_overlay" / "wrong_evidence_preserved.json"
        ),
        "original_parsed_clob_snapshot": _load_json_any(
            view / "correction_overlay" / "original_parsed_clob_snapshot.json"
        ),
    }
    return _canonical_sha256(material)


def _corrected_overlay_view_index_sha256(view: Path) -> str:
    """Deterministic index over relative paths + file SHA256 within the overlay view."""

    rows: list[dict[str, str]] = []
    for path in sorted(view.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(view).as_posix()
        rows.append({"path": rel, "sha256": _file_sha256_path(path)})
    return _canonical_sha256(rows)


def _raw_overlay_index_hash(correction_namespace: Path) -> str:
    ledger_path = correction_namespace / "ledger.jsonl"
    if not ledger_path.is_file():
        return ""
    ledger = AppendOnlyLedger(ledger_path)
    return _raw_index_sha256(ledger)


def _load_json_any(path: Path) -> Any:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
