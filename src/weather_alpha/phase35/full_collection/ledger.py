"""Append-only request ledger and resume rules. No provider network."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from weather_alpha.models.timeutil import ensure_utc
from weather_alpha.phase35.full_collection.policy import (
    HASH_ALGORITHM,
    HTTP_METHOD,
    PARSER_SCHEMA_VERSION,
)
from weather_alpha.phase35.full_collection.provenance import (
    RawProbeResult,
    assert_canonical_path_safe,
    atomic_write_bytes,
)


class ResultClassification(StrEnum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    VALID_EMPTY = "VALID_EMPTY"
    PROVIDER_NO_DATA = "PROVIDER_NO_DATA"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    TLS_FAILURE = "TLS_FAILURE"
    TRANSIENT_TRANSPORT_FAILURE = "TRANSIENT_TRANSPORT_FAILURE"
    TRANSIENT_5XX = "TRANSIENT_5XX"
    HTTP_FAILURE = "HTTP_FAILURE"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    INELIGIBLE = "INELIGIBLE"
    SKIPPED_ALREADY_COMPLETE = "SKIPPED_ALREADY_COMPLETE"
    INTERRUPTED_RESUMABLE = "INTERRUPTED_RESUMABLE"


COMPLETE_REUSABLE = frozenset(
    {
        ResultClassification.SUCCESS,
        ResultClassification.VALID_EMPTY,
        ResultClassification.PROVIDER_NO_DATA,
        ResultClassification.INELIGIBLE,
        ResultClassification.SKIPPED_ALREADY_COMPLETE,
    }
)
TRANSIENT_CLASSES = frozenset(
    {
        ResultClassification.RATE_LIMITED,
        ResultClassification.TIMEOUT,
        ResultClassification.TLS_FAILURE,
        ResultClassification.TRANSIENT_TRANSPORT_FAILURE,
        ResultClassification.TRANSIENT_5XX,
    }
)
NO_DATA_CLASSES = frozenset(
    {
        ResultClassification.VALID_EMPTY,
        ResultClassification.PROVIDER_NO_DATA,
    }
)


class DuplicateIdentityError(ValueError):
    """Raised when a completed identity would be replayed as a new success."""


class RawProvenanceHashFailure(RuntimeError):
    """Fail-closed when a completed identity's raw hash does not match."""


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    collection_id: str
    provider: str
    endpoint: str
    http_method: str
    canonical_request_identity: str
    normalized_request_parameters: dict[str, Any]
    attempt_number: int
    attempt_timestamp_utc: datetime
    latency_ms: float | None
    http_status: int | None
    retry_after_seconds: float | None
    content_sha256: str | None
    stable_raw_provenance_path: str | None
    parser_schema_version: str
    result_classification: ResultClassification
    error_class: str | None = None
    error_detail: str | None = None
    collection_status: str | None = None
    hash_algorithm: str = HASH_ALGORITHM

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempt_timestamp_utc", ensure_utc(self.attempt_timestamp_utc))
        if self.http_method != HTTP_METHOD:
            raise ValueError("ledger http_method must be GET")
        if self.stable_raw_provenance_path:
            assert_canonical_path_safe(self.stable_raw_provenance_path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "attempt_timestamp_utc": self.attempt_timestamp_utc.isoformat(),
            "canonical_request_identity": self.canonical_request_identity,
            "collection_id": self.collection_id,
            "collection_status": self.collection_status,
            "content_sha256": self.content_sha256,
            "endpoint": self.endpoint,
            "error_class": self.error_class,
            "error_detail": self.error_detail,
            "hash_algorithm": self.hash_algorithm,
            "http_method": self.http_method,
            "http_status": self.http_status,
            "latency_ms": self.latency_ms,
            "normalized_request_parameters": dict(self.normalized_request_parameters),
            "parser_schema_version": self.parser_schema_version,
            "provider": self.provider,
            "result_classification": self.result_classification.value,
            "retry_after_seconds": self.retry_after_seconds,
            "stable_raw_provenance_path": self.stable_raw_provenance_path,
        }


def record_from_dict(payload: dict[str, Any]) -> LedgerRecord:
    return LedgerRecord(
        collection_id=str(payload["collection_id"]),
        provider=str(payload["provider"]),
        endpoint=str(payload["endpoint"]),
        http_method=str(payload["http_method"]),
        canonical_request_identity=str(payload["canonical_request_identity"]),
        normalized_request_parameters=dict(payload.get("normalized_request_parameters") or {}),
        attempt_number=int(payload["attempt_number"]),
        attempt_timestamp_utc=datetime.fromisoformat(str(payload["attempt_timestamp_utc"])),
        latency_ms=None if payload.get("latency_ms") is None else float(payload["latency_ms"]),
        http_status=None if payload.get("http_status") is None else int(payload["http_status"]),
        retry_after_seconds=(
            None
            if payload.get("retry_after_seconds") is None
            else float(payload["retry_after_seconds"])
        ),
        content_sha256=None
        if payload.get("content_sha256") is None
        else str(payload["content_sha256"]),
        stable_raw_provenance_path=(
            None
            if payload.get("stable_raw_provenance_path") is None
            else str(payload["stable_raw_provenance_path"])
        ),
        parser_schema_version=str(payload.get("parser_schema_version") or PARSER_SCHEMA_VERSION),
        result_classification=ResultClassification(str(payload["result_classification"])),
        error_class=None if payload.get("error_class") is None else str(payload["error_class"]),
        error_detail=None if payload.get("error_detail") is None else str(payload["error_detail"]),
        collection_status=(
            None if payload.get("collection_status") is None else str(payload["collection_status"])
        ),
        hash_algorithm=str(payload.get("hash_algorithm") or HASH_ALGORITHM),
    )


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    skip: bool
    classification: ResultClassification | None
    next_attempt_number: int
    reason: str
    fail_closed: bool = False


class AppendOnlyLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: list[LedgerRecord] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._records.append(record_from_dict(json.loads(line)))

    def records(self) -> tuple[LedgerRecord, ...]:
        return tuple(self._records)

    def records_for(self, identity: str) -> tuple[LedgerRecord, ...]:
        return tuple(row for row in self._records if row.canonical_request_identity == identity)

    def append(self, record: LedgerRecord) -> LedgerRecord:
        existing = self.records_for(record.canonical_request_identity)
        if record.result_classification in {
            ResultClassification.SUCCESS,
            ResultClassification.VALID_EMPTY,
        } and any(row.result_classification in COMPLETE_REUSABLE for row in existing):
            raise DuplicateIdentityError(
                f"duplicate success replay blocked for {record.canonical_request_identity}"
            )
        encoded = json.dumps(
            record.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            current = self.path.read_bytes()
            payload = current + (encoded + "\n").encode("utf-8")
        else:
            payload = (encoded + "\n").encode("utf-8")
        atomic_write_bytes(self.path, payload)
        self._records.append(record)
        return record

    def resume_decision(
        self,
        identity: str,
        *,
        parser_schema_version: str,
        probe: RawProbeResult | None,
    ) -> ResumeDecision:
        rows = self.records_for(identity)
        if not rows:
            return ResumeDecision(
                skip=False,
                classification=None,
                next_attempt_number=1,
                reason="no_prior_attempts",
            )
        complete = [
            row
            for row in rows
            if row.result_classification in COMPLETE_REUSABLE
            and row.result_classification != ResultClassification.SKIPPED_ALREADY_COMPLETE
        ]
        if complete:
            latest = complete[-1]
            if latest.parser_schema_version != parser_schema_version:
                return ResumeDecision(
                    skip=False,
                    classification=None,
                    next_attempt_number=max(row.attempt_number for row in rows) + 1,
                    reason="parser_incompatible",
                    fail_closed=True,
                )
            if (
                latest.content_sha256
                and latest.stable_raw_provenance_path
                and (probe is None or not probe.exists)
            ):
                return ResumeDecision(
                    skip=False,
                    classification=None,
                    next_attempt_number=latest.attempt_number,
                    reason="raw_missing",
                    fail_closed=True,
                )
            if (
                latest.content_sha256
                and latest.stable_raw_provenance_path
                and probe is not None
                and not probe.hash_matches
            ):
                raise RawProvenanceHashFailure(
                    f"raw hash mismatch for {identity}: expected {latest.content_sha256}"
                )
            return ResumeDecision(
                skip=True,
                classification=ResultClassification.SKIPPED_ALREADY_COMPLETE,
                next_attempt_number=latest.attempt_number,
                reason="already_complete",
            )
        last = rows[-1]
        if last.result_classification is ResultClassification.PENDING:
            return ResumeDecision(
                skip=False,
                classification=None,
                next_attempt_number=last.attempt_number,
                reason="interrupted_pending",
            )
        if last.result_classification is ResultClassification.INTERRUPTED_RESUMABLE:
            return ResumeDecision(
                skip=False,
                classification=last.result_classification,
                next_attempt_number=last.attempt_number,
                reason=last.error_class or "INTERRUPTED_RESUMABLE",
                fail_closed=True,
            )
        return ResumeDecision(
            skip=False,
            classification=None,
            next_attempt_number=last.attempt_number + 1,
            reason="retry_or_continue",
        )
