"""Unit-testable GET attempt machinery. Blocked when preflight fails. No live collector."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_alpha.http.readonly import (
    ReadOnlyHttpClient,
    ReadOnlyResponse,
    RetryExhaustedError,
)
from weather_alpha.models.timeutil import utc_now
from weather_alpha.phase35.full_collection.budget import (
    BudgetEnforcement,
    DiskProbe,
    StaticDiskProbe,
    provider_budget_bucket,
)
from weather_alpha.phase35.full_collection.ledger import (
    AppendOnlyLedger,
    DuplicateIdentityError,
    LedgerRecord,
    RawProvenanceHashFailure,
    ResultClassification,
)
from weather_alpha.phase35.full_collection.policy import (
    GLOBAL_GET_ATTEMPT_CAP,
    HTTP_METHOD,
    PARSER_SCHEMA_VERSION,
    REQUEST_BUDGET_REDESIGN_REQUIRED,
    STORAGE_HARD_CAP_BYTES,
    TIMEOUT_SECONDS,
    YES_PENDING_FINAL_REVIEW,
)
from weather_alpha.phase35.full_collection.provenance import (
    persist_raw_atomically,
    probe_raw,
)
from weather_alpha.phase35.full_collection.retry import (
    attempts_exhausted,
    classify_exception,
    classify_http_outcome,
    is_retryable,
    retry_delay_seconds,
)


class CollectionPreflightBlocked(RuntimeError):
    def __init__(self, status: str = REQUEST_BUDGET_REDESIGN_REQUIRED) -> None:
        super().__init__(f"collection blocked: {status}")
        self.status = status


class CollectionCapExceeded(RuntimeError):
    COLLECTION_STATUS = "INTERRUPTED_RESUMABLE"

    def __init__(self, cap: str, *, record: LedgerRecord | None = None) -> None:
        super().__init__(
            f"COLLECTION_STATUS={self.COLLECTION_STATUS} fail-closed collection cap: {cap}"
        )
        self.cap = cap
        self.collection_status = self.COLLECTION_STATUS
        self.record = record


@dataclass(frozen=True, slots=True)
class PlannedGet:
    identity: str
    provider: str
    endpoint: str
    day: str
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    record: LedgerRecord
    skipped: bool
    delay_seconds: float | None = None


class BoundedGetExecutor:
    """Issues GET via ReadOnlyHttpClient only when preflight allows.

    This is not a provider collector. Full-plan preflight that fails closed
    must not call the transport.
    """

    def __init__(
        self,
        *,
        collection_id: str,
        http: ReadOnlyHttpClient,
        ledger: AppendOnlyLedger,
        raw_root: Path,
        enforcement: BudgetEnforcement,
        disk: DiskProbe | None = None,
        global_attempt_cap: int = GLOBAL_GET_ATTEMPT_CAP,
        storage_hard_cap_bytes: int = STORAGE_HARD_CAP_BYTES,
        timeout_seconds: float = TIMEOUT_SECONDS,
        sleeper: Any = None,
        clock: Any = None,
    ) -> None:
        self.collection_id = collection_id
        self._http = http
        self._ledger = ledger
        self._raw_root = raw_root
        self._enforcement = enforcement
        self._disk = disk or StaticDiskProbe(
            free_bytes_value=storage_hard_cap_bytes, used_bytes_value=0
        )
        self._global_attempt_cap = global_attempt_cap
        self._storage_hard_cap_bytes = storage_hard_cap_bytes
        self._timeout_seconds = timeout_seconds
        self._sleeper = sleeper or (lambda _seconds: None)
        self._clock = clock or utc_now
        estimate = enforcement.estimate
        self._provider_caps = {
            "gamma": estimate.gamma_max_attempts,
            "ecmwf": estimate.ecmwf_max_attempts,
            "clob": estimate.clob_max_attempts,
        }
        self._provider_reserves = {
            "gamma": estimate.gamma_retry_reserve,
            "ecmwf": estimate.ecmwf_retry_reserve,
            "clob": estimate.clob_retry_reserve,
        }
        self._provider_attempts = {"gamma": 0, "ecmwf": 0, "clob": 0}
        self._provider_retries = {"gamma": 0, "ecmwf": 0, "clob": 0}
        self._get_attempts = 0
        self._hydrate_attempt_counts()

    def execute(self, planned: PlannedGet, *, schema_ok: bool | None = None) -> AttemptOutcome:
        if not self._enforcement.allowed:
            raise CollectionPreflightBlocked(self._enforcement.status)
        if not self._enforcement.network_authorized:
            raise CollectionPreflightBlocked(
                self._enforcement.full_collection_start_allowed or YES_PENDING_FINAL_REVIEW
            )
        used = self._disk.used_bytes(self._raw_root)
        if used >= self._storage_hard_cap_bytes:
            record = self._persist_interrupted(planned, attempt_number=1, reason="storage_hard_cap")
            raise CollectionCapExceeded("storage_hard_cap", record=record)
        resume = self._ledger.resume_decision(
            planned.identity,
            parser_schema_version=PARSER_SCHEMA_VERSION,
            probe=_probe_for(self._ledger, planned.identity, self._raw_root),
        )
        if resume.fail_closed:
            raise CollectionCapExceeded(resume.reason)
        if resume.skip and resume.classification is ResultClassification.SKIPPED_ALREADY_COMPLETE:
            record = LedgerRecord(
                collection_id=self.collection_id,
                provider=planned.provider,
                endpoint=planned.endpoint,
                http_method=HTTP_METHOD,
                canonical_request_identity=planned.identity,
                normalized_request_parameters=dict(planned.params),
                attempt_number=resume.next_attempt_number,
                attempt_timestamp_utc=self._clock(),
                latency_ms=0.0,
                http_status=None,
                retry_after_seconds=None,
                content_sha256=_latest_hash(self._ledger, planned.identity),
                stable_raw_provenance_path=_latest_path(self._ledger, planned.identity),
                parser_schema_version=PARSER_SCHEMA_VERSION,
                result_classification=ResultClassification.SKIPPED_ALREADY_COMPLETE,
            )
            self._ledger.append(record)
            return AttemptOutcome(record=record, skipped=True)
        attempt_number = resume.next_attempt_number
        prior = _latest_attempt(self._ledger, planned.identity)
        if attempt_number > MAX_GUARD:
            record = self._persist_interrupted(
                planned,
                attempt_number=attempt_number,
                reason="retry_exhausted",
                prior=None if prior is None else prior.result_classification,
            )
            raise CollectionCapExceeded("retry_exhausted", record=record)
        if attempt_number > 1:
            if prior is None or not is_retryable(
                prior.result_classification, http_status=prior.http_status
            ):
                record = self._persist_interrupted(
                    planned,
                    attempt_number=attempt_number,
                    reason="retry_exhausted",
                    prior=None if prior is None else prior.result_classification,
                )
                raise CollectionCapExceeded("retry_exhausted", record=record)
            if attempts_exhausted(prior.attempt_number):
                record = self._persist_interrupted(
                    planned,
                    attempt_number=attempt_number,
                    reason="retry_exhausted",
                    prior=prior.result_classification,
                )
                raise CollectionCapExceeded("retry_exhausted", record=record)
        bucket = provider_budget_bucket(planned.provider)
        if attempt_number > 1 and self._provider_retries[bucket] >= self._provider_reserves[bucket]:
            record = self._persist_interrupted(
                planned,
                attempt_number=attempt_number,
                reason="retry_reserve_exhausted",
                bucket=bucket,
                prior=None if prior is None else prior.result_classification,
            )
            raise CollectionCapExceeded("retry_reserve_exhausted", record=record)
        if self._provider_attempts[bucket] >= self._provider_caps[bucket]:
            record = self._persist_interrupted(
                planned,
                attempt_number=attempt_number,
                reason="provider_attempt_cap_exhausted",
                bucket=bucket,
                prior=None if prior is None else prior.result_classification,
            )
            raise CollectionCapExceeded("provider_attempt_cap_exhausted", record=record)
        if self._get_attempts >= self._global_attempt_cap:
            record = self._persist_interrupted(
                planned,
                attempt_number=attempt_number,
                reason="global_attempt_cap_exhausted",
                bucket=bucket,
                prior=None if prior is None else prior.result_classification,
            )
            raise CollectionCapExceeded("global_attempt_cap_exhausted", record=record)
        pending = LedgerRecord(
            collection_id=self.collection_id,
            provider=planned.provider,
            endpoint=planned.endpoint,
            http_method=HTTP_METHOD,
            canonical_request_identity=planned.identity,
            normalized_request_parameters=dict(planned.params),
            attempt_number=attempt_number,
            attempt_timestamp_utc=self._clock(),
            latency_ms=None,
            http_status=None,
            retry_after_seconds=None,
            content_sha256=None,
            stable_raw_provenance_path=None,
            parser_schema_version=PARSER_SCHEMA_VERSION,
            result_classification=ResultClassification.PENDING,
        )
        self._ledger.append(pending)
        started = self._clock()
        try:
            self._get_attempts += 1
            self._provider_attempts[bucket] += 1
            if attempt_number > 1:
                self._provider_retries[bucket] += 1
            response = self._http.get(
                planned.endpoint,
                params=planned.params,
                timeout=self._timeout_seconds,
            )
        except DuplicateIdentityError:
            raise
        except RawProvenanceHashFailure:
            raise
        except Exception as exc:
            target: BaseException = exc
            if isinstance(exc, RetryExhaustedError) and exc.__cause__ is not None:
                target = exc.__cause__
            classification = classify_exception(target)
            record = _complete(
                pending,
                classification=classification,
                clock=started,
                error_class=type(target).__name__,
                error_detail=str(target),
            )
            self._replace_pending(pending, record)
            delay = retry_delay_seconds(None) if is_retryable(classification) else None
            if delay is not None and not attempts_exhausted(attempt_number):
                self._sleeper(delay)
            return AttemptOutcome(record=record, skipped=False, delay_seconds=delay)
        payload = _payload_from(response)
        retry_after = _parse_retry_after(response.headers)
        classification = classify_http_outcome(
            status_code=response.status_code,
            payload=payload,
            schema_ok=schema_ok,
        )
        persisted_path = None
        digest = None
        if classification in {
            ResultClassification.SUCCESS,
            ResultClassification.VALID_EMPTY,
            ResultClassification.PROVIDER_NO_DATA,
            ResultClassification.SCHEMA_ERROR,
        }:
            persisted = persist_raw_atomically(
                self._raw_root, provider=planned.provider, day=planned.day, payload=payload
            )
            persisted_path = persisted.stable_path
            digest = persisted.content_sha256
        latency = max((self._clock() - started).total_seconds() * 1000.0, 0.0)
        record = _complete(
            pending,
            classification=classification,
            clock=started,
            http_status=response.status_code,
            retry_after_seconds=retry_after,
            latency_ms=latency,
            content_sha256=digest,
            stable_raw_provenance_path=persisted_path,
        )
        self._replace_pending(pending, record)
        delay = None
        if is_retryable(
            classification, http_status=response.status_code
        ) and not attempts_exhausted(attempt_number):
            delay = retry_delay_seconds(retry_after)
            self._sleeper(delay)
        return AttemptOutcome(record=record, skipped=False, delay_seconds=delay)

    def _replace_pending(self, pending: LedgerRecord, completed: LedgerRecord) -> None:
        # Append-only: keep the PENDING row and append the terminal row.
        del pending
        self._ledger.append(completed)

    def _hydrate_attempt_counts(self) -> None:
        skip = {
            ResultClassification.SKIPPED_ALREADY_COMPLETE,
            ResultClassification.INTERRUPTED_RESUMABLE,
            ResultClassification.PENDING,
        }
        for row in self._ledger.records():
            if row.result_classification in skip:
                continue
            bucket = provider_budget_bucket(row.provider)
            self._get_attempts += 1
            self._provider_attempts[bucket] += 1
            if row.attempt_number > 1:
                self._provider_retries[bucket] += 1

    def _persist_interrupted(
        self,
        planned: PlannedGet,
        *,
        attempt_number: int,
        reason: str,
        bucket: str | None = None,
        prior: ResultClassification | None = None,
    ) -> LedgerRecord:
        budget_bucket = bucket if bucket is not None else provider_budget_bucket(planned.provider)
        evidence = {
            "stop_reason": reason,
            "provider": planned.provider,
            "provider_attempts": self._provider_attempts[budget_bucket],
            "provider_cap": self._provider_caps[budget_bucket],
            "retry_attempts": self._provider_retries[budget_bucket],
            "retry_reserve": self._provider_reserves[budget_bucket],
            "request_identity": planned.identity,
            "attempt_number": attempt_number,
            "global_attempts": self._get_attempts,
            "global_cap": self._global_attempt_cap,
        }
        if prior is not None:
            evidence["prior_classification"] = prior.value
        detail = json.dumps(evidence, sort_keys=True)
        record = LedgerRecord(
            collection_id=self.collection_id,
            provider=planned.provider,
            endpoint=planned.endpoint,
            http_method=HTTP_METHOD,
            canonical_request_identity=planned.identity,
            normalized_request_parameters=dict(planned.params),
            attempt_number=attempt_number,
            attempt_timestamp_utc=self._clock(),
            latency_ms=None,
            http_status=None,
            retry_after_seconds=None,
            content_sha256=None,
            stable_raw_provenance_path=None,
            parser_schema_version=PARSER_SCHEMA_VERSION,
            result_classification=ResultClassification.INTERRUPTED_RESUMABLE,
            error_class=reason,
            error_detail=detail,
            collection_status=CollectionCapExceeded.COLLECTION_STATUS,
        )
        self._ledger.append(record)
        return record


MAX_GUARD = 8


def _complete(
    pending: LedgerRecord,
    *,
    classification: ResultClassification,
    clock: datetime,
    http_status: int | None = None,
    retry_after_seconds: float | None = None,
    latency_ms: float | None = None,
    content_sha256: str | None = None,
    stable_raw_provenance_path: str | None = None,
    error_class: str | None = None,
    error_detail: str | None = None,
) -> LedgerRecord:
    del clock
    return LedgerRecord(
        collection_id=pending.collection_id,
        provider=pending.provider,
        endpoint=pending.endpoint,
        http_method=pending.http_method,
        canonical_request_identity=pending.canonical_request_identity,
        normalized_request_parameters=dict(pending.normalized_request_parameters),
        attempt_number=pending.attempt_number,
        attempt_timestamp_utc=pending.attempt_timestamp_utc,
        latency_ms=latency_ms,
        http_status=http_status,
        retry_after_seconds=retry_after_seconds,
        content_sha256=content_sha256,
        stable_raw_provenance_path=stable_raw_provenance_path,
        parser_schema_version=pending.parser_schema_version,
        result_classification=classification,
        error_class=error_class,
        error_detail=error_detail,
    )


def _payload_from(response: ReadOnlyResponse) -> Any:
    try:
        return response.json()
    except Exception:
        text = response.text()
        return {"_unparsed": text} if text else {}


def _parse_retry_after(headers: Any) -> float | None:
    raw = None
    if headers:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _latest_attempt(ledger: AppendOnlyLedger, identity: str) -> LedgerRecord | None:
    rows = [
        row
        for row in ledger.records_for(identity)
        if row.result_classification
        not in {
            ResultClassification.PENDING,
            ResultClassification.INTERRUPTED_RESUMABLE,
            ResultClassification.SKIPPED_ALREADY_COMPLETE,
        }
    ]
    return None if not rows else rows[-1]


def _latest_hash(ledger: AppendOnlyLedger, identity: str) -> str | None:
    rows = [row for row in ledger.records_for(identity) if row.content_sha256]
    return None if not rows else rows[-1].content_sha256


def _latest_path(ledger: AppendOnlyLedger, identity: str) -> str | None:
    rows = [row for row in ledger.records_for(identity) if row.stable_raw_provenance_path]
    return None if not rows else rows[-1].stable_raw_provenance_path


def _probe_for(ledger: AppendOnlyLedger, identity: str, raw_root: Path) -> Any:
    rows = [
        row
        for row in ledger.records_for(identity)
        if row.content_sha256 and row.stable_raw_provenance_path
    ]
    if not rows:
        return None
    latest = rows[-1]
    provenance_path = latest.stable_raw_provenance_path
    content_sha256 = latest.content_sha256
    if provenance_path is None or content_sha256 is None:
        raise TypeError("stable_raw_provenance_path and content_sha256 are required")
    runtime = raw_root / Path(provenance_path)
    return probe_raw(runtime, content_sha256)
