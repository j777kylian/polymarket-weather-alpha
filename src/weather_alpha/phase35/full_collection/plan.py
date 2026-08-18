"""Offline plan/contract validation. Makes no provider calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_alpha.phase35.full_collection.audit import (
    DatasetAuditResult,
    DatasetObservation,
    ExpectedCell,
    audit_dataset,
    build_dataset_audit_reports,
    build_dataset_freeze,
)
from weather_alpha.phase35.full_collection.budget import (
    DiskProbe,
    RealDiskProbe,
    enforce_request_budget,
    request_budget_report,
)
from weather_alpha.phase35.full_collection.manifest import (
    ManifestCreateResult,
    create_immutable_manifest,
)
from weather_alpha.phase35.full_collection.policy import (
    REQUEST_BUDGET_REDESIGN_REQUIRED,
    THEORETICAL_ENVELOPE_AUTHORIZATION,
    YES_PENDING_FINAL_REVIEW,
)
from weather_alpha.phase35.full_collection.provenance import atomic_write_json
from weather_alpha.research.reports import write_report_pair


@dataclass(frozen=True, slots=True)
class PlanValidationResult:
    status: str
    network_authorized: bool
    collection_started: bool
    full_collection_start_allowed: str
    theoretical_envelope_authorized: bool
    manifest: ManifestCreateResult

    def as_dict(self) -> dict[str, Any]:
        estimate = self.manifest.enforcement.estimate
        return {
            "FULL_COLLECTION_START_ALLOWED": self.full_collection_start_allowed,
            "REQUEST_BUDGET": request_budget_report(estimate),
            "collection_started": self.collection_started,
            "manifest": self.manifest.as_dict(),
            "network_authorized": self.network_authorized,
            "status": self.status,
            "theoretical_envelope": THEORETICAL_ENVELOPE_AUTHORIZATION,
            "theoretical_envelope_authorized": self.theoretical_envelope_authorized,
        }


def validate_full_collection_plan(
    *,
    manifest_path: Path | None = None,
    code_commit: str | None = None,
    created_at: datetime | None = None,
    disk: DiskProbe | None = None,
    storage_root: Path | None = None,
) -> PlanValidationResult:
    probe = disk or RealDiskProbe()
    created = create_immutable_manifest(
        destination=manifest_path,
        code_commit=code_commit,
        created_at=created_at,
        disk=probe,
        storage_root=storage_root,
    )
    return PlanValidationResult(
        status=created.status,
        network_authorized=False,
        collection_started=False,
        full_collection_start_allowed=created.enforcement.full_collection_start_allowed,
        theoretical_envelope_authorized=created.enforcement.theoretical_envelope_authorized,
        manifest=created,
    )


def refuse_historical_collection(plan: PlanValidationResult | None = None) -> dict[str, Any]:
    enforcement = plan.manifest.enforcement if plan is not None else enforce_request_budget()
    if enforcement.allowed:
        return {
            "FULL_COLLECTION_START_ALLOWED": enforcement.full_collection_start_allowed,
            "REQUEST_BUDGET": request_budget_report(enforcement.estimate),
            "collection_started": False,
            "network_authorized": False,
            "reason": (
                "pending final review; not a collection execution grant; "
                "theoretical envelope is NOT_AUTHORIZED"
            ),
            "status": YES_PENDING_FINAL_REVIEW,
            "theoretical_envelope": THEORETICAL_ENVELOPE_AUTHORIZATION,
            "theoretical_envelope_authorized": False,
        }
    return {
        "collection_started": False,
        "network_authorized": False,
        "reason": (
            "preflight blocked; live collection is not runnable under "
            + REQUEST_BUDGET_REDESIGN_REQUIRED
        ),
        "status": REQUEST_BUDGET_REDESIGN_REQUIRED,
        "violated_caps": list(enforcement.violated_caps),
    }


def run_offline_dataset_acceptance(
    *,
    output_dir: Path,
    expected: tuple[ExpectedCell, ...] | list[ExpectedCell] = (),
    observations: tuple[DatasetObservation, ...] | list[DatasetObservation] = (),
    collection_id: str = "uncollected",
    code_commit: str = "unknown",
    manifest_sha256: str = "none",
) -> DatasetAuditResult:
    audit = audit_dataset(expected=expected, observations=observations)
    machine, human = build_dataset_audit_reports(audit)
    write_report_pair(
        output_dir / "reports" / "phase35_dataset_acceptance.md",
        output_dir / "reports" / "phase35_dataset_acceptance.json",
        human,
        machine,
    )
    freeze = build_dataset_freeze(
        audit,
        collection_id=collection_id,
        code_commit=code_commit,
        manifest_sha256=manifest_sha256,
        raw_index_sha256="none",
        canonical_dataset_sha256="none",
        report_sha256="none",
        date_range={"end": "uncollected", "start": "uncollected"},
        event_count=0,
        snapshot_count=0,
        checkpoint_counts={},
        city_counts={},
        missingness_summary={"status": "uncollected"},
        quarantine_summary={"status": "uncollected"},
    )
    if freeze is not None:
        atomic_write_json(output_dir / "reports" / "phase35_dataset_freeze.json", freeze.as_dict())
    return audit
