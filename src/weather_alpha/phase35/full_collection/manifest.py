"""Immutable pre-network collection manifest. Fail-closed on budget conflict."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_alpha.models.timeutil import utc_now
from weather_alpha.phase35.full_collection.budget import (
    BudgetEnforcement,
    DiskProbe,
    StaticDiskProbe,
    enforce_request_budget,
    estimate_full_collection_budget,
    request_budget_report,
)
from weather_alpha.phase35.full_collection.policy import (
    CANDIDATE_SELECTION_RULE,
    CHECKPOINTS,
    CLOB_ATTEMPT_CAP,
    CLOB_ENDPOINT,
    CONCURRENCY,
    ECMWF_ATTEMPT_CAP,
    ECMWF_ENDPOINT,
    ECMWF_LOGICAL_IDENTITY_CAP,
    END_DATE,
    ENDPOINT_ALLOWLIST,
    FORECAST_AVAILABILITY_RULE,
    FORECAST_MODEL,
    FORECAST_PROVIDER,
    GAMMA_ATTEMPT_CAP,
    GAMMA_ENDPOINT,
    GLOBAL_GET_ATTEMPT_CAP,
    HASH_ALGORITHM,
    HTTP_METHOD,
    INTER_ATTEMPT_DELAY_SECONDS,
    MARKET_PROVIDER,
    MAX_ATTEMPTS_PER_IDENTITY,
    MAX_RETRIES,
    PARSER_SCHEMA_VERSION,
    PRICE_PROVIDER,
    PRICE_SELECTION_RULE,
    RAW_STORAGE_NAMESPACE,
    REQUEST_BUDGET_REDESIGN_REQUIRED,
    REQUEST_POLICY_VERSION,
    RETRY_AFTER_CAP_SECONDS,
    RETRY_MODE,
    RETRY_ONLY,
    RETRYABLE_HTTP_STATUSES,
    SCHEMA_VERSION,
    START_DATE,
    STORAGE_HARD_CAP_BYTES,
    STORAGE_PREFLIGHT_MIN_BYTES,
    TARGET_CITIES_CANONICAL,
    TIMEOUT_SECONDS,
)
from weather_alpha.phase35.full_collection.provenance import (
    assert_text_has_no_machine_roots,
    atomic_write_json,
)
from weather_alpha.phase35.full_collection.schedule import catalog_stations, station_catalog_payload


def resolve_code_commit(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "unknown"
    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    return "unknown"


def policy_payload() -> dict[str, Any]:
    return {
        "CHECKPOINTS": list(CHECKPOINTS),
        "CITIES": list(TARGET_CITIES_CANONICAL),
        "END_DATE": END_DATE,
        "FORECAST_AVAILABILITY_RULE": FORECAST_AVAILABILITY_RULE,
        "FORECAST_MODEL": FORECAST_MODEL,
        "FORECAST_PROVIDER": FORECAST_PROVIDER,
        "MARKET_PROVIDER": MARKET_PROVIDER,
        "PRICE_PROVIDER": PRICE_PROVIDER,
        "RAW_STORAGE_NAMESPACE": RAW_STORAGE_NAMESPACE,
        "RATE_LIMIT_POLICY": {
            "concurrency": CONCURRENCY,
            "inter_attempt_delay_seconds": INTER_ATTEMPT_DELAY_SECONDS,
            "retry_after_cap_seconds": RETRY_AFTER_CAP_SECONDS,
        },
        "REQUEST_POLICY": {
            "concurrency": CONCURRENCY,
            "endpoint_allowlist": list(ENDPOINT_ALLOWLIST),
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
        "START_DATE": START_DATE,
    }


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256((canonical_json(payload) + "\n").encode("utf-8")).hexdigest()


def derive_collection_id(*, code_commit: str, created_at: datetime) -> str:
    material = {
        "code_commit": code_commit,
        "created_at": created_at.isoformat(),
        "policy": policy_payload(),
        "schema_version": SCHEMA_VERSION,
    }
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()[:12]
    return f"phase35-hist-{START_DATE}-{END_DATE}-{digest}"


@dataclass(frozen=True, slots=True)
class ManifestCreateResult:
    status: str
    authorized: bool
    collection_id: str | None
    manifest_sha256: str | None
    written: bool
    enforcement: BudgetEnforcement
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "authorized": self.authorized,
            "collection_id": self.collection_id,
            "enforcement": self.enforcement.as_dict(),
            "manifest_sha256": self.manifest_sha256,
            "payload": self.payload,
            "status": self.status,
            "written": self.written,
        }


def build_manifest_payload(
    *,
    code_commit: str,
    created_at: datetime,
    stations: list[dict[str, Any]],
    enforcement: BudgetEnforcement,
) -> dict[str, Any]:
    collection_id = derive_collection_id(code_commit=code_commit, created_at=created_at)
    payload: dict[str, Any] = {
        "CANDIDATE_SELECTION_RULE": CANDIDATE_SELECTION_RULE,
        "CHECKPOINTS": list(CHECKPOINTS),
        "CITIES": list(TARGET_CITIES_CANONICAL),
        "CLOB_ENDPOINT": CLOB_ENDPOINT,
        "CODE_COMMIT": code_commit,
        "COLLECTION_ID": collection_id,
        "CREATED_AT": created_at.isoformat(),
        "ECMWF_ENDPOINT": ECMWF_ENDPOINT,
        "END_DATE": END_DATE,
        "ENDPOINT_ALLOWLIST": list(ENDPOINT_ALLOWLIST),
        "FORECAST_AVAILABILITY_RULE": FORECAST_AVAILABILITY_RULE,
        "FORECAST_MODEL": FORECAST_MODEL,
        "FORECAST_PROVIDER": FORECAST_PROVIDER,
        "GAMMA_ENDPOINT": GAMMA_ENDPOINT,
        "FULL_COLLECTION_START_ALLOWED": enforcement.full_collection_start_allowed,
        "HASH_ALGORITHM": HASH_ALGORITHM,
        "MARKET_PROVIDER": MARKET_PROVIDER,
        "PARSER_SCHEMA_VERSION": PARSER_SCHEMA_VERSION,
        "PREFLIGHT": enforcement.as_dict(),
        "PRICE_PROVIDER": PRICE_PROVIDER,
        "PRICE_SELECTION_RULE": PRICE_SELECTION_RULE,
        "RATE_LIMIT_POLICY": {
            "concurrency": CONCURRENCY,
            "inter_attempt_delay_seconds": INTER_ATTEMPT_DELAY_SECONDS,
            "retry_after_cap_seconds": RETRY_AFTER_CAP_SECONDS,
        },
        "RAW_STORAGE_NAMESPACE": RAW_STORAGE_NAMESPACE,
        "REQUEST_BUDGET": request_budget_report(enforcement.estimate),
        "REQUEST_CAPS": {
            "clob_attempts": CLOB_ATTEMPT_CAP,
            "ecmwf_attempts": ECMWF_ATTEMPT_CAP,
            "ecmwf_logical_identities": ECMWF_LOGICAL_IDENTITY_CAP,
            "gamma_attempts": GAMMA_ATTEMPT_CAP,
            "global_get_attempts": GLOBAL_GET_ATTEMPT_CAP,
            "storage_hard_cap_bytes": STORAGE_HARD_CAP_BYTES,
            "storage_preflight_min_bytes": STORAGE_PREFLIGHT_MIN_BYTES,
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
        "SCHEMA_VERSION": SCHEMA_VERSION,
        "START_DATE": START_DATE,
        "STATIONS": stations,
        "TRACK": "HISTORICAL_DESCRIPTIVE",
        "price_semantics": "DESCRIPTIVE_ONLY",
    }
    serialized = canonical_json(payload)
    assert_text_has_no_machine_roots(serialized)
    return payload


def create_immutable_manifest(
    *,
    destination: Path | None = None,
    code_commit: str | None = None,
    created_at: datetime | None = None,
    disk: DiskProbe | None = None,
    storage_root: Path | None = None,
) -> ManifestCreateResult:
    created = created_at or utc_now()
    commit = resolve_code_commit(code_commit)
    estimate = estimate_full_collection_budget()
    probe = disk or StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES)
    enforcement = enforce_request_budget(estimate, disk=probe, storage_root=storage_root)
    stations = station_catalog_payload(catalog_stations())
    payload = build_manifest_payload(
        code_commit=commit,
        created_at=created,
        stations=stations,
        enforcement=enforcement,
    )
    if not enforcement.allowed:
        plan = {
            "authorized": False,
            "collection_id": payload["COLLECTION_ID"],
            "enforcement": enforcement.as_dict(),
            "manifest_authorized": False,
            "policy": policy_payload(),
            "status": REQUEST_BUDGET_REDESIGN_REQUIRED,
        }
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(destination, plan)
        return ManifestCreateResult(
            status=REQUEST_BUDGET_REDESIGN_REQUIRED,
            authorized=False,
            collection_id=payload["COLLECTION_ID"],
            manifest_sha256=None,
            written=False,
            enforcement=enforcement,
            payload=plan,
        )
    if destination is None:
        raise ValueError("authorized manifest requires a destination path")
    if destination.is_file():
        raise ValueError(f"immutable manifest already exists: {destination.name}")
    digest = atomic_write_json(destination, payload)
    return ManifestCreateResult(
        status=enforcement.status,
        authorized=True,
        collection_id=payload["COLLECTION_ID"],
        manifest_sha256=digest,
        written=True,
        enforcement=enforcement,
        payload=payload,
    )
