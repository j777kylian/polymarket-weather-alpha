"""Production dataset freeze from persisted collection artifacts.

Offline: consumes a completed collection namespace, recomputes audit, and binds
real hashes. Does not contact providers, create manifests, or authorize collection.
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
from weather_alpha.phase35.full_collection.policy import END_DATE, START_DATE
from weather_alpha.phase35.full_collection.provenance import (
    assert_text_has_no_machine_roots,
    atomic_write_json,
    probe_raw,
)

FREEZE_ARTIFACT_NAME = "phase35_dataset_freeze.json"
HISTORICAL_AUDIT_JSON = "phase35_historical_audit.json"
FREEZE_ARTIFACT_RELATIVE = f"reports/{FREEZE_ARTIFACT_NAME}"
AUDIT_ARTIFACT_RELATIVE = f"reports/{HISTORICAL_AUDIT_JSON}"
PLACEHOLDER_HASHES = frozenset({"", "none", "uncollected", "0" * 64})


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
        resolved_commit = str(verified.get("CODE_COMMIT") or resolved_commit)
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
