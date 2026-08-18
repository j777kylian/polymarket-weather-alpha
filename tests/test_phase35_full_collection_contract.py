"""Pre-network Phase 3.5 full historical collection contract tests.

Fakes and local temp files only. No provider network, daemon, Phase 4, or trading.
"""

from __future__ import annotations

import json
import ssl
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from tests.fakes import RecordingGetTransport
from weather_alpha.cli import main
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyHttpError, ReadOnlyResponse
from weather_alpha.phase35.checkpoints import ForecastCandidate
from weather_alpha.phase35.full_collection.audit import (
    DatasetObservation,
    ExpectedCell,
    audit_dataset,
    build_dataset_freeze,
    point_in_time_flags,
)
from weather_alpha.phase35.full_collection.budget import (
    BudgetEnforcement,
    BudgetEstimate,
    StaticDiskProbe,
    enforce_request_budget,
    estimate_full_collection_budget,
    request_budget_report,
)
from weather_alpha.phase35.full_collection.executor import (
    BoundedGetExecutor,
    CollectionCapExceeded,
    CollectionPreflightBlocked,
    PlannedGet,
)
from weather_alpha.phase35.full_collection.ledger import (
    AppendOnlyLedger,
    DuplicateIdentityError,
    LedgerRecord,
    RawProvenanceHashFailure,
    ResultClassification,
)
from weather_alpha.phase35.full_collection.manifest import create_immutable_manifest
from weather_alpha.phase35.full_collection.plan import (
    refuse_historical_collection,
    run_offline_dataset_acceptance,
    validate_full_collection_plan,
)
from weather_alpha.phase35.full_collection.policy import (
    CHECKPOINTS,
    CLOB_ATTEMPT_CAP,
    CLOB_ENDPOINT,
    CONCURRENCY,
    END_DATE,
    FORECAST_MODEL,
    FORECAST_PROVIDER,
    GAMMA_ATTEMPT_CAP,
    GLOBAL_GET_ATTEMPT_CAP,
    INTER_ATTEMPT_DELAY_SECONDS,
    MAX_ATTEMPTS_PER_IDENTITY,
    MAX_RETRIES,
    PARSER_SCHEMA_VERSION,
    PREFLIGHT_OK,
    REQUEST_BUDGET_REDESIGN_REQUIRED,
    REQUEST_POLICY_VERSION,
    RETRY_AFTER_CAP_SECONDS,
    START_DATE,
    STORAGE_HARD_CAP_BYTES,
    STORAGE_PREFLIGHT_MIN_BYTES,
    TARGET_CITIES_CANONICAL,
    TIMEOUT_SECONDS,
    YES_PENDING_FINAL_REVIEW,
)
from weather_alpha.phase35.full_collection.provenance import (
    persist_raw_atomically,
    probe_raw,
    stable_historical_raw_provenance_path,
)
from weather_alpha.phase35.full_collection.retry import (
    classify_http_outcome,
    is_retryable,
    retry_delay_seconds,
)
from weather_alpha.phase35.full_collection.schedule import inclusive_date_strings
from weather_alpha.research.prices import PricePoint


def _fixed_clock() -> datetime:
    return datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _interruption_evidence(record: LedgerRecord) -> dict[str, Any]:
    payload = json.loads(record.error_detail or "{}")
    assert isinstance(payload, dict)
    return payload


def _mini_estimate(**overrides: Any) -> BudgetEstimate:
    defaults = {
        "gamma_identities": 1,
        "clob_identities": 1,
        "ecmwf_logical_identities": 1,
        "gamma_initial_attempts": 1,
        "clob_initial_attempts": 1,
        "ecmwf_initial_attempts": 1,
        "initial_total_attempts": 3,
        "gamma_retry_reserve": 1,
        "ecmwf_retry_reserve": 1,
        "clob_retry_reserve": 1,
        "gamma_max_attempts": 2,
        "clob_max_attempts": 2,
        "ecmwf_max_attempts": 2,
        "global_max_attempts": 6,
        "theoretical_gamma_attempts": 2,
        "theoretical_ecmwf_attempts": 2,
        "theoretical_clob_attempts": 2,
        "theoretical_global_attempts": 6,
        "theoretical_envelope_authorized": False,
        "max_attempts_per_identity": 2,
        "planning_baseline_ecmwf_logical": 4829,
        "computed_ecmwf_logical": 1,
    }
    unexpected = [key for key in overrides if key not in defaults]
    if unexpected:
        raise TypeError(
            f"BudgetEstimate.__init__() got an unexpected keyword argument '{unexpected[0]}'"
        )
    return BudgetEstimate(
        gamma_identities=overrides.get("gamma_identities", defaults["gamma_identities"]),
        clob_identities=overrides.get("clob_identities", defaults["clob_identities"]),
        ecmwf_logical_identities=overrides.get(
            "ecmwf_logical_identities", defaults["ecmwf_logical_identities"]
        ),
        gamma_initial_attempts=overrides.get(
            "gamma_initial_attempts", defaults["gamma_initial_attempts"]
        ),
        clob_initial_attempts=overrides.get(
            "clob_initial_attempts", defaults["clob_initial_attempts"]
        ),
        ecmwf_initial_attempts=overrides.get(
            "ecmwf_initial_attempts", defaults["ecmwf_initial_attempts"]
        ),
        initial_total_attempts=overrides.get(
            "initial_total_attempts", defaults["initial_total_attempts"]
        ),
        gamma_retry_reserve=overrides.get("gamma_retry_reserve", defaults["gamma_retry_reserve"]),
        ecmwf_retry_reserve=overrides.get("ecmwf_retry_reserve", defaults["ecmwf_retry_reserve"]),
        clob_retry_reserve=overrides.get("clob_retry_reserve", defaults["clob_retry_reserve"]),
        gamma_max_attempts=overrides.get("gamma_max_attempts", defaults["gamma_max_attempts"]),
        clob_max_attempts=overrides.get("clob_max_attempts", defaults["clob_max_attempts"]),
        ecmwf_max_attempts=overrides.get("ecmwf_max_attempts", defaults["ecmwf_max_attempts"]),
        global_max_attempts=overrides.get("global_max_attempts", defaults["global_max_attempts"]),
        theoretical_gamma_attempts=overrides.get(
            "theoretical_gamma_attempts", defaults["theoretical_gamma_attempts"]
        ),
        theoretical_ecmwf_attempts=overrides.get(
            "theoretical_ecmwf_attempts", defaults["theoretical_ecmwf_attempts"]
        ),
        theoretical_clob_attempts=overrides.get(
            "theoretical_clob_attempts", defaults["theoretical_clob_attempts"]
        ),
        theoretical_global_attempts=overrides.get(
            "theoretical_global_attempts", defaults["theoretical_global_attempts"]
        ),
        theoretical_envelope_authorized=overrides.get(
            "theoretical_envelope_authorized", defaults["theoretical_envelope_authorized"]
        ),
        max_attempts_per_identity=overrides.get(
            "max_attempts_per_identity", defaults["max_attempts_per_identity"]
        ),
        planning_baseline_ecmwf_logical=overrides.get(
            "planning_baseline_ecmwf_logical", defaults["planning_baseline_ecmwf_logical"]
        ),
        computed_ecmwf_logical=overrides.get(
            "computed_ecmwf_logical", defaults["computed_ecmwf_logical"]
        ),
    )


def _mini_enforcement(**estimate_overrides: Any) -> BudgetEnforcement:
    estimate = _mini_estimate(**estimate_overrides)
    return BudgetEnforcement(
        allowed=True,
        status=PREFLIGHT_OK,
        network_authorized=True,
        full_collection_start_allowed=YES_PENDING_FINAL_REVIEW,
        theoretical_envelope_authorized=False,
        violated_caps=(),
        estimate=estimate,
        storage_preflight_ok=True,
        detail=(),
    )


def _ledger_record(
    identity: str,
    classification: ResultClassification,
    *,
    attempt: int = 1,
    sha: str | None = None,
    path: str | None = None,
    status: int | None = 200,
) -> LedgerRecord:
    return LedgerRecord(
        collection_id="test-collection",
        provider="polymarket_clob_prices_history",
        endpoint=CLOB_ENDPOINT,
        http_method="GET",
        canonical_request_identity=identity,
        normalized_request_parameters={"market": "t1"},
        attempt_number=attempt,
        attempt_timestamp_utc=_fixed_clock(),
        latency_ms=1.0,
        http_status=status,
        retry_after_seconds=None,
        content_sha256=sha,
        stable_raw_provenance_path=path,
        parser_schema_version=PARSER_SCHEMA_VERSION,
        result_classification=classification,
    )


def _executor(
    tmp_path: Path,
    transport: Any,
    *,
    enforcement: BudgetEnforcement | None = None,
    disk: StaticDiskProbe | None = None,
    global_cap: int = 100,
    sleeper: Any = None,
) -> tuple[BoundedGetExecutor, AppendOnlyLedger]:
    ledger = AppendOnlyLedger(tmp_path / "ledger.jsonl")
    http = ReadOnlyHttpClient(
        transport=transport,
        max_retries=0,
        retry_statuses=frozenset(),
        sleeper=lambda _s: None,
    )
    executor = BoundedGetExecutor(
        collection_id="test-collection",
        http=http,
        ledger=ledger,
        raw_root=tmp_path,
        enforcement=enforcement if enforcement is not None else _mini_enforcement(),
        disk=disk
        or StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
        global_attempt_cap=global_cap,
        sleeper=sleeper or (lambda _s: None),
        clock=_fixed_clock,
    )
    return executor, ledger


def _planned(identity: str = "clob:paris:2026-03-01") -> PlannedGet:
    return PlannedGet(
        identity=identity,
        provider="polymarket_clob_prices_history",
        endpoint="https://clob.polymarket.com/prices-history",
        day="2026-03-01",
        params={"market": "t1"},
    )


def test_frozen_request_policy_is_immutable_binding() -> None:
    assert START_DATE == "2026-03-01"
    assert END_DATE == "2026-05-29"
    assert TARGET_CITIES_CANONICAL == (
        "amsterdam",
        "london",
        "milan",
        "munich",
        "new york",
        "paris",
    )
    assert CHECKPOINTS == (48, 24, 12, 6, 3, 1)
    assert FORECAST_PROVIDER == "open_meteo_single_runs"
    assert FORECAST_MODEL == "ecmwf_ifs"
    assert REQUEST_POLICY_VERSION == "phase35-full-collection-request-policy-v2"
    assert TIMEOUT_SECONDS == 35.0
    assert CONCURRENCY == 1
    assert INTER_ATTEMPT_DELAY_SECONDS == 2.0
    assert MAX_RETRIES == 1
    assert MAX_ATTEMPTS_PER_IDENTITY == 2
    assert RETRY_AFTER_CAP_SECONDS == 60.0
    assert GAMMA_ATTEMPT_CAP == 600
    assert CLOB_ATTEMPT_CAP == 600
    assert GLOBAL_GET_ATTEMPT_CAP == 11200
    assert STORAGE_PREFLIGHT_MIN_BYTES == 2 * 1024**3
    assert STORAGE_HARD_CAP_BYTES == 3 * 1024**3
    assert YES_PENDING_FINAL_REVIEW == "YES_PENDING_FINAL_REVIEW"
    assert len(inclusive_date_strings()) == 90


def test_request_budget_policy_v2_classification_triggered_not_identity_times_two() -> None:
    estimate = estimate_full_collection_budget()
    assert estimate.gamma_identities == 540
    assert estimate.ecmwf_logical_identities == 4829
    assert estimate.clob_identities == 540
    assert estimate.gamma_initial_attempts == 540
    assert estimate.ecmwf_initial_attempts == 4829
    assert estimate.clob_initial_attempts == 540
    assert estimate.initial_total_attempts == 5909
    assert estimate.gamma_retry_reserve == 60
    assert estimate.ecmwf_retry_reserve == 5171
    assert estimate.clob_retry_reserve == 60
    assert estimate.gamma_max_attempts == 600
    assert estimate.ecmwf_max_attempts == 9658
    assert estimate.clob_max_attempts == 600
    assert estimate.global_max_attempts == 10858
    assert estimate.theoretical_gamma_attempts == 1080
    assert estimate.theoretical_ecmwf_attempts == 9658
    assert estimate.theoretical_clob_attempts == 1080
    assert estimate.theoretical_global_attempts == 11818
    assert estimate.theoretical_envelope_authorized is False
    assert estimate.clob_max_attempts == CLOB_ATTEMPT_CAP
    assert estimate.gamma_max_attempts == GAMMA_ATTEMPT_CAP
    assert estimate.theoretical_clob_attempts > CLOB_ATTEMPT_CAP
    assert estimate.theoretical_gamma_attempts > GAMMA_ATTEMPT_CAP
    assert estimate.theoretical_global_attempts > GLOBAL_GET_ATTEMPT_CAP
    report = request_budget_report(estimate)
    assert report["initial"] == {"gamma": 540, "ecmwf": 4829, "clob": 540, "total": 5909}
    assert report["retry_reserves"] == {"gamma": 60, "ecmwf": 5171, "clob": 60}
    assert report["cap_bounded_maxima"] == {
        "gamma": 600,
        "ecmwf": 9658,
        "clob": 600,
        "global": 10858,
    }
    assert report["theoretical_all_transient_once"] == {
        "gamma": 1080,
        "ecmwf": 9658,
        "clob": 1080,
        "global": 11818,
    }
    assert report["theoretical_envelope"] == "NOT_AUTHORIZED"
    assert report["theoretical_envelope_authorized"] is False
    enforcement = enforce_request_budget(
        estimate,
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES),
    )
    assert enforcement.allowed is True
    assert enforcement.status == PREFLIGHT_OK
    assert enforcement.full_collection_start_allowed == YES_PENDING_FINAL_REVIEW
    assert enforcement.network_authorized is False
    assert enforcement.theoretical_envelope_authorized is False
    assert enforcement.violated_caps == ()


def test_manifest_creation_pending_review_without_network(tmp_path: Path) -> None:
    dest = tmp_path / "manifests" / "plan.json"
    result = create_immutable_manifest(
        destination=dest,
        code_commit="test-commit",
        created_at=_fixed_clock(),
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES),
    )
    assert result.status == PREFLIGHT_OK
    assert result.authorized is True
    assert result.written is True
    assert result.manifest_sha256 is not None
    assert result.enforcement.network_authorized is False
    assert result.enforcement.full_collection_start_allowed == YES_PENDING_FINAL_REVIEW
    assert result.enforcement.theoretical_envelope_authorized is False
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert payload["REQUEST_POLICY"]["version"] == REQUEST_POLICY_VERSION
    assert payload["PREFLIGHT"]["full_collection_start_allowed"] == YES_PENDING_FINAL_REVIEW
    assert payload["PREFLIGHT"]["theoretical_envelope_authorized"] is False
    assert payload["PREFLIGHT"]["network_authorized"] is False
    text = dest.read_text(encoding="utf-8")
    for leak in ("/tmp/", "/Users/", "/home/"):
        assert leak not in text


def test_full_plan_executor_makes_no_provider_call(tmp_path: Path) -> None:
    transport = RecordingGetTransport({"/prices-history": {"history": [{"t": 1, "p": 0.2}]}})
    enforcement = enforce_request_budget(
        estimate_full_collection_budget(),
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES),
    )
    executor, _ledger = _executor(tmp_path, transport, enforcement=enforcement)
    with pytest.raises(CollectionPreflightBlocked, match=YES_PENDING_FINAL_REVIEW):
        executor.execute(_planned())
    assert transport.calls == []


def test_restart_interruption_does_not_duplicate_success(tmp_path: Path) -> None:
    transport = RecordingGetTransport({"/prices-history": {"history": [{"t": 1, "p": 0.4}]}})
    executor, ledger = _executor(tmp_path, transport)
    ledger.append(
        _ledger_record("clob:paris:2026-03-01", ResultClassification.PENDING, status=None, sha=None)
    )
    outcome = executor.execute(_planned())
    assert outcome.record.result_classification is ResultClassification.SUCCESS
    assert (
        len(
            [
                row
                for row in ledger.records_for("clob:paris:2026-03-01")
                if row.result_classification is ResultClassification.SUCCESS
            ]
        )
        == 1
    )
    skipped = executor.execute(_planned())
    assert skipped.skipped is True
    assert skipped.record.result_classification is ResultClassification.SKIPPED_ALREADY_COMPLETE
    assert len(transport.calls) == 1


def test_corrupted_raw_hash_fail_closed(tmp_path: Path) -> None:
    transport = RecordingGetTransport({"/prices-history": {"history": [{"t": 1, "p": 0.4}]}})
    executor, _ledger = _executor(tmp_path, transport)
    first = executor.execute(_planned())
    assert first.record.stable_raw_provenance_path is not None
    runtime = tmp_path / Path(first.record.stable_raw_provenance_path)
    runtime.write_text('{"history":[{"t":9,"p":0.9}]}\n', encoding="utf-8")
    with pytest.raises(RawProvenanceHashFailure):
        executor.execute(_planned())
    assert len(transport.calls) == 1


def test_duplicate_identity_success_rejected(tmp_path: Path) -> None:
    ledger = AppendOnlyLedger(tmp_path / "ledger.jsonl")
    persisted = persist_raw_atomically(
        tmp_path,
        provider="polymarket_clob_prices_history",
        day="2026-03-01",
        payload={"history": [{"t": 1, "p": 0.2}]},
    )
    first = _ledger_record(
        "clob:paris:2026-03-01",
        ResultClassification.SUCCESS,
        sha=persisted.content_sha256,
        path=persisted.stable_path,
    )
    ledger.append(first)
    with pytest.raises(DuplicateIdentityError):
        ledger.append(first)


def _status_transport(status_code: int, *, body: bytes = b"{}") -> Any:
    class StatusTransport:
        def __init__(self) -> None:
            self.calls = 0

        def get(
            self,
            url: str,
            *,
            params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> ReadOnlyResponse:
            del params, headers, timeout
            self.calls += 1
            return ReadOnlyResponse(status_code=status_code, url=url, headers={}, content=body)

    return StatusTransport()


def test_retry_exhaustion_after_two_attempts(tmp_path: Path) -> None:
    class Always429:
        def get(
            self,
            url: str,
            *,
            params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> ReadOnlyResponse:
            del params, headers, timeout
            return ReadOnlyResponse(status_code=429, url=url, headers={}, content=b"{}")

    transport = Always429()
    executor, _ledger = _executor(tmp_path, transport)
    first = executor.execute(_planned())
    assert first.record.result_classification is ResultClassification.RATE_LIMITED
    second = executor.execute(_planned())
    assert second.record.result_classification is ResultClassification.RATE_LIMITED
    with pytest.raises(CollectionCapExceeded, match="retry_exhausted") as exhausted:
        executor.execute(_planned())
    assert exhausted.value.COLLECTION_STATUS == "INTERRUPTED_RESUMABLE"
    assert exhausted.value.collection_status == "INTERRUPTED_RESUMABLE"
    interrupt_rows = [
        row
        for row in _ledger.records()
        if row.result_classification is ResultClassification.INTERRUPTED_RESUMABLE
    ]
    assert interrupt_rows
    assert interrupt_rows[-1].collection_status == "INTERRUPTED_RESUMABLE"
    assert interrupt_rows[-1].error_class == "retry_exhausted"


def test_429_retry_after_capped_at_60s(tmp_path: Path) -> None:
    class Once429:
        def __init__(self) -> None:
            self.calls = 0

        def get(
            self,
            url: str,
            *,
            params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> ReadOnlyResponse:
            del params, headers, timeout
            self.calls += 1
            if self.calls == 1:
                return ReadOnlyResponse(
                    status_code=429,
                    url=url,
                    headers={"Retry-After": "120"},
                    content=b"{}",
                )
            return ReadOnlyResponse(
                status_code=200,
                url=url,
                headers={},
                content=b'{"history":[{"t":1,"p":0.3}]}',
            )

    sleeps: list[float] = []
    transport = Once429()
    executor, _ledger = _executor(tmp_path, transport, sleeper=sleeps.append)
    limited = executor.execute(_planned())
    assert limited.record.result_classification is ResultClassification.RATE_LIMITED
    assert limited.delay_seconds == 60.0
    assert sleeps == [60.0]
    success = executor.execute(_planned())
    assert success.record.result_classification is ResultClassification.SUCCESS
    assert retry_delay_seconds(120) == 60.0


def test_timeout_distinct_from_no_data(tmp_path: Path) -> None:
    class TimeoutTransport:
        def get(
            self,
            url: str,
            *,
            params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> ReadOnlyResponse:
            del url, params, headers, timeout
            raise TimeoutError("read timed out")

    executor, _ledger = _executor(tmp_path, TimeoutTransport())
    outcome = executor.execute(_planned())
    timeout_cls: ResultClassification = outcome.record.result_classification
    no_data_cls: ResultClassification = ResultClassification.PROVIDER_NO_DATA
    assert timeout_cls is ResultClassification.TIMEOUT
    assert timeout_cls is not no_data_cls
    assert outcome.delay_seconds == 2.0


def test_tls_failure_distinct_from_no_data(tmp_path: Path) -> None:
    class TlsTransport:
        def get(
            self,
            url: str,
            *,
            params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> ReadOnlyResponse:
            del url, params, headers, timeout
            raise ssl.SSLError("certificate verify failed")

    executor, _ledger = _executor(tmp_path, TlsTransport())
    outcome = executor.execute(_planned())
    tls_cls: ResultClassification = outcome.record.result_classification
    no_data_cls: ResultClassification = ResultClassification.PROVIDER_NO_DATA
    assert tls_cls is ResultClassification.TLS_FAILURE
    assert tls_cls is not no_data_cls


def test_success_no_data_valid_empty_are_one_attempt(tmp_path: Path) -> None:
    success_transport = RecordingGetTransport(
        {"/prices-history": {"history": [{"t": 1, "p": 0.2}]}}
    )
    executor, _ledger = _executor(tmp_path / "success", success_transport)
    first = executor.execute(_planned("clob:paris:2026-03-01"))
    assert first.record.result_classification is ResultClassification.SUCCESS
    skipped = executor.execute(_planned("clob:paris:2026-03-01"))
    assert skipped.skipped is True
    assert skipped.record.result_classification is ResultClassification.SKIPPED_ALREADY_COMPLETE
    assert len(success_transport.calls) == 1
    assert is_retryable(ResultClassification.SUCCESS) is False

    no_data = RecordingGetTransport({"/prices-history": {"reason": "no data"}})
    executor_nd, _ = _executor(tmp_path / "nodata", no_data)
    nd = executor_nd.execute(_planned("clob:london:2026-03-01"))
    assert nd.record.result_classification is ResultClassification.PROVIDER_NO_DATA
    nd_skip = executor_nd.execute(_planned("clob:london:2026-03-01"))
    assert nd_skip.skipped is True
    assert len(no_data.calls) == 1
    assert is_retryable(ResultClassification.PROVIDER_NO_DATA) is False

    empty = RecordingGetTransport({"/prices-history": {"history": []}})
    executor_empty, _ = _executor(tmp_path / "empty", empty)
    ev = executor_empty.execute(_planned("clob:milan:2026-03-01"))
    assert ev.record.result_classification is ResultClassification.VALID_EMPTY
    ev_skip = executor_empty.execute(_planned("clob:milan:2026-03-01"))
    assert ev_skip.skipped is True
    assert len(empty.calls) == 1
    assert is_retryable(ResultClassification.VALID_EMPTY) is False


def test_429_timeout_tls_at_most_two_attempts(tmp_path: Path) -> None:
    limited = _status_transport(429)
    executor, ledger = _executor(tmp_path / "rl", limited)
    assert executor.execute(_planned("clob:paris:2026-03-01")).record.result_classification is (
        ResultClassification.RATE_LIMITED
    )
    assert executor.execute(_planned("clob:paris:2026-03-01")).record.result_classification is (
        ResultClassification.RATE_LIMITED
    )
    with pytest.raises(CollectionCapExceeded, match="retry_exhausted") as cap:
        executor.execute(_planned("clob:paris:2026-03-01"))
    assert cap.value.COLLECTION_STATUS == "INTERRUPTED_RESUMABLE"
    assert limited.calls == 2
    assert any(
        row.result_classification is ResultClassification.INTERRUPTED_RESUMABLE
        for row in ledger.records()
    )

    class AlwaysTimeout:
        def __init__(self) -> None:
            self.calls = 0

        def get(
            self,
            url: str,
            *,
            params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> ReadOnlyResponse:
            del url, params, headers, timeout
            self.calls += 1
            raise TimeoutError("read timed out")

    timeout_transport = AlwaysTimeout()
    executor_t, _ = _executor(tmp_path / "timeout", timeout_transport)
    planned_t = _planned("clob:london:2026-03-01")
    assert (
        executor_t.execute(planned_t).record.result_classification is ResultClassification.TIMEOUT
    )
    assert (
        executor_t.execute(planned_t).record.result_classification is ResultClassification.TIMEOUT
    )
    with pytest.raises(CollectionCapExceeded, match="retry_exhausted"):
        executor_t.execute(planned_t)
    assert timeout_transport.calls == 2

    class AlwaysTls:
        def __init__(self) -> None:
            self.calls = 0

        def get(
            self,
            url: str,
            *,
            params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> ReadOnlyResponse:
            del url, params, headers, timeout
            self.calls += 1
            raise ssl.SSLError("certificate verify failed")

    tls_transport = AlwaysTls()
    executor_tls, _ = _executor(tmp_path / "tls", tls_transport)
    planned_tls = _planned("clob:milan:2026-03-01")
    assert (
        executor_tls.execute(planned_tls).record.result_classification
        is ResultClassification.TLS_FAILURE
    )
    assert (
        executor_tls.execute(planned_tls).record.result_classification
        is ResultClassification.TLS_FAILURE
    )
    with pytest.raises(CollectionCapExceeded, match="retry_exhausted"):
        executor_tls.execute(planned_tls)
    assert tls_transport.calls == 2


def test_permanent_outcomes_are_never_retried(tmp_path: Path) -> None:
    assert classify_http_outcome(status_code=404, payload={}) is ResultClassification.HTTP_FAILURE
    assert is_retryable(ResultClassification.HTTP_FAILURE, http_status=404) is False
    assert is_retryable(ResultClassification.SCHEMA_ERROR) is False
    assert is_retryable(ResultClassification.INELIGIBLE) is False
    assert classify_http_outcome(status_code=503, payload={}) is ResultClassification.TRANSIENT_5XX
    assert is_retryable(ResultClassification.TRANSIENT_5XX, http_status=503) is True

    not_found = _status_transport(404)
    executor, _ = _executor(tmp_path / "404", not_found)
    planned = _planned("clob:paris:2026-03-01")
    assert executor.execute(planned).record.result_classification is (
        ResultClassification.HTTP_FAILURE
    )
    with pytest.raises(CollectionCapExceeded, match="retry_exhausted"):
        executor.execute(planned)
    assert not_found.calls == 1

    schema_transport = RecordingGetTransport({"/prices-history": {"history": [{"t": 1, "p": 0.2}]}})
    executor_s, _ = _executor(tmp_path / "schema", schema_transport)
    planned_s = _planned("clob:london:2026-03-01")
    assert (
        executor_s.execute(planned_s, schema_ok=False).record.result_classification
        is ResultClassification.SCHEMA_ERROR
    )
    with pytest.raises(CollectionCapExceeded, match="retry_exhausted"):
        executor_s.execute(planned_s, schema_ok=False)
    assert len(schema_transport.calls) == 1


def test_transient_transport_distinct_from_no_data_and_permanent_http(tmp_path: Path) -> None:
    class ConnectFail:
        def __init__(self) -> None:
            self.calls = 0

        def get(
            self,
            url: str,
            *,
            params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> ReadOnlyResponse:
            del url, params, headers, timeout
            self.calls += 1
            raise httpx.ConnectError(
                "connection refused",
                request=httpx.Request("GET", "https://clob.polymarket.com/prices-history"),
            )

    transport = ConnectFail()
    executor, _ = _executor(tmp_path, transport)
    planned = _planned()
    first = executor.execute(planned)
    transport_cls: ResultClassification = first.record.result_classification
    assert transport_cls is ResultClassification.TRANSIENT_TRANSPORT_FAILURE
    assert transport_cls not in {
        ResultClassification.PROVIDER_NO_DATA,
        ResultClassification.HTTP_FAILURE,
    }
    assert is_retryable(transport_cls) is True
    second = executor.execute(planned)
    assert second.record.result_classification is ResultClassification.TRANSIENT_TRANSPORT_FAILURE
    with pytest.raises(CollectionCapExceeded, match="retry_exhausted"):
        executor.execute(planned)
    assert transport.calls == 2


def test_retry_reserve_exhaustion_is_interrupted_resumable_with_ledger_missingness(
    tmp_path: Path,
) -> None:
    transport = _status_transport(429)
    enforcement = _mini_enforcement(
        clob_identities=2,
        clob_initial_attempts=2,
        clob_retry_reserve=1,
        clob_max_attempts=3,
        global_max_attempts=10,
        theoretical_clob_attempts=4,
        theoretical_global_attempts=8,
    )
    executor, ledger = _executor(tmp_path, transport, enforcement=enforcement, global_cap=10)
    first_id = _planned("clob:paris:2026-03-01")
    second_id = _planned("clob:london:2026-03-01")
    assert executor.execute(first_id).record.result_classification is (
        ResultClassification.RATE_LIMITED
    )
    assert executor.execute(first_id).record.result_classification is (
        ResultClassification.RATE_LIMITED
    )
    assert executor.execute(second_id).record.result_classification is (
        ResultClassification.RATE_LIMITED
    )
    with pytest.raises(CollectionCapExceeded, match="retry_reserve_exhausted") as interrupted:
        executor.execute(second_id)
    assert interrupted.value.COLLECTION_STATUS == "INTERRUPTED_RESUMABLE"
    assert interrupted.value.collection_status == "INTERRUPTED_RESUMABLE"
    assert interrupted.value.cap == "retry_reserve_exhausted"
    assert transport.calls == 3
    rate_limited = [
        row
        for row in ledger.records()
        if row.result_classification is ResultClassification.RATE_LIMITED
    ]
    interrupt_rows = [
        row
        for row in ledger.records()
        if row.result_classification is ResultClassification.INTERRUPTED_RESUMABLE
    ]
    assert len(rate_limited) >= 3
    assert interrupt_rows
    interrupt = interrupt_rows[-1]
    assert interrupt.canonical_request_identity == "clob:london:2026-03-01"
    assert interrupt.collection_status == "INTERRUPTED_RESUMABLE"
    assert interrupt.error_class == "retry_reserve_exhausted"
    assert interrupt.error_class != "clob_attempts"
    evidence = _interruption_evidence(interrupt)
    assert evidence["stop_reason"] == "retry_reserve_exhausted"
    assert evidence["provider"] == "polymarket_clob_prices_history"
    assert evidence["provider_attempts"] == 3
    assert evidence["provider_cap"] == 3
    assert evidence["retry_attempts"] == 1
    assert evidence["retry_reserve"] == 1
    assert evidence["request_identity"] == "clob:london:2026-03-01"
    assert evidence["attempt_number"] == 2
    assert evidence["prior_classification"] == "RATE_LIMITED"


def test_provider_attempt_cap_exhausted_on_initial_request(tmp_path: Path) -> None:
    transport = RecordingGetTransport({"/prices-history": {"history": [{"t": 1, "p": 0.1}]}})
    enforcement = _mini_enforcement(
        clob_identities=3,
        clob_initial_attempts=3,
        clob_retry_reserve=0,
        clob_max_attempts=2,
        global_max_attempts=10,
        theoretical_clob_attempts=3,
        theoretical_global_attempts=8,
    )
    executor, ledger = _executor(tmp_path, transport, enforcement=enforcement, global_cap=10)
    assert executor.execute(_planned("clob:paris:2026-03-01")).record.result_classification is (
        ResultClassification.SUCCESS
    )
    assert executor.execute(_planned("clob:london:2026-03-01")).record.result_classification is (
        ResultClassification.SUCCESS
    )
    with pytest.raises(
        CollectionCapExceeded, match="provider_attempt_cap_exhausted"
    ) as interrupted:
        executor.execute(_planned("clob:milan:2026-03-01"))
    assert interrupted.value.COLLECTION_STATUS == "INTERRUPTED_RESUMABLE"
    assert interrupted.value.cap == "provider_attempt_cap_exhausted"
    assert len(transport.calls) == 2
    interrupt_rows = [
        row
        for row in ledger.records()
        if row.result_classification is ResultClassification.INTERRUPTED_RESUMABLE
    ]
    assert interrupt_rows
    interrupt = interrupt_rows[-1]
    assert interrupt.error_class == "provider_attempt_cap_exhausted"
    assert interrupt.error_class != "clob_attempts"
    assert interrupt.attempt_number == 1
    evidence = _interruption_evidence(interrupt)
    assert evidence["stop_reason"] == "provider_attempt_cap_exhausted"
    assert evidence["provider"] == "polymarket_clob_prices_history"
    assert evidence["provider_attempts"] == 2
    assert evidence["provider_cap"] == 2
    assert evidence["retry_attempts"] == 0
    assert evidence["retry_reserve"] == 0
    assert evidence["request_identity"] == "clob:milan:2026-03-01"
    assert evidence["attempt_number"] == 1


def test_provider_no_data_distinct_from_valid_empty(tmp_path: Path) -> None:
    assert (
        classify_http_outcome(status_code=200, payload={"reason": "no data"})
        is ResultClassification.PROVIDER_NO_DATA
    )
    assert (
        classify_http_outcome(status_code=200, payload={"history": []})
        is ResultClassification.VALID_EMPTY
    )
    no_data = RecordingGetTransport({"/prices-history": {"reason": "no data"}})
    executor, _ledger = _executor(tmp_path, no_data)
    got = executor.execute(_planned("clob:london:2026-03-01"))
    got_cls: ResultClassification = got.record.result_classification
    assert got_cls is ResultClassification.PROVIDER_NO_DATA
    empty = RecordingGetTransport({"/prices-history": {"history": []}})
    executor_b, _ = _executor(tmp_path / "b", empty)
    empty_out = executor_b.execute(_planned("clob:london:2026-03-02"))
    empty_cls: ResultClassification = empty_out.record.result_classification
    assert empty_cls is not got_cls
    assert empty_cls is ResultClassification.VALID_EMPTY


def test_storage_hard_cap_fail_closed(tmp_path: Path) -> None:
    transport = RecordingGetTransport({"/prices-history": {"history": [{"t": 1, "p": 0.1}]}})
    executor, _ledger = _executor(
        tmp_path,
        transport,
        disk=StaticDiskProbe(
            free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=STORAGE_HARD_CAP_BYTES
        ),
    )
    with pytest.raises(CollectionCapExceeded, match="storage_hard_cap"):
        executor.execute(_planned())
    assert transport.calls == []


def test_global_get_cap_fail_closed(tmp_path: Path) -> None:
    transport = RecordingGetTransport({"/prices-history": {"history": [{"t": 1, "p": 0.1}]}})
    executor, ledger = _executor(tmp_path, transport, global_cap=1)
    executor.execute(_planned("clob:paris:2026-03-01"))
    with pytest.raises(CollectionCapExceeded, match="global_attempt_cap_exhausted") as exceeded:
        executor.execute(_planned("clob:london:2026-03-01"))
    assert exceeded.value.COLLECTION_STATUS == "INTERRUPTED_RESUMABLE"
    assert exceeded.value.cap == "global_attempt_cap_exhausted"
    assert len(transport.calls) == 1
    interrupt_rows = [
        row
        for row in ledger.records()
        if row.result_classification is ResultClassification.INTERRUPTED_RESUMABLE
    ]
    assert interrupt_rows
    interrupt = interrupt_rows[-1]
    assert interrupt.error_class == "global_attempt_cap_exhausted"
    assert interrupt.error_class != "global_get_attempts"
    assert interrupt.collection_status == "INTERRUPTED_RESUMABLE"
    evidence = _interruption_evidence(interrupt)
    assert evidence["stop_reason"] == "global_attempt_cap_exhausted"
    assert evidence["provider"] == "polymarket_clob_prices_history"
    assert evidence["request_identity"] == "clob:london:2026-03-01"
    assert evidence["attempt_number"] == 1
    assert evidence["global_attempts"] == 1
    assert evidence["global_cap"] == 1


def test_post_decision_forecast_and_price_are_leakage() -> None:
    decision_event = "2026-03-15"
    future_forecast = ForecastCandidate(
        issued_at=datetime(2026, 3, 15, 12, tzinfo=UTC),
        available_at=datetime(2026, 3, 15, 18, tzinfo=UTC),
        run_param="2026-03-15T12:00",
    )
    future_price = PricePoint(observed_at=datetime(2026, 3, 16, tzinfo=UTC), price=0.4)
    forecast_leak, _price_ok, reasons_f = point_in_time_flags(
        event_date=decision_event,
        timezone_name="Europe/Paris",
        lead_hours=24,
        canonical_event_key=("event_id", "e1"),
        forecasts=(future_forecast,),
        prices=(PricePoint(observed_at=datetime(2026, 3, 13, tzinfo=UTC), price=0.2),),
    )
    _forecast_ok, price_leak, reasons_p = point_in_time_flags(
        event_date=decision_event,
        timezone_name="Europe/Paris",
        lead_hours=24,
        canonical_event_key=("event_id", "e1"),
        forecasts=(
            ForecastCandidate(
                issued_at=datetime(2026, 3, 13, 0, tzinfo=UTC),
                available_at=datetime(2026, 3, 13, 6, tzinfo=UTC),
                run_param="2026-03-13T00:00",
            ),
        ),
        prices=(future_price,),
    )
    assert forecast_leak is True
    assert "forecast_future_only" in reasons_f
    assert price_leak is True
    assert "price_post_decision_only" in reasons_p


def _obs(
    *,
    day: str,
    city: str,
    checkpoint: int,
    family: str,
    observed: bool = True,
    usable: bool = True,
    missing: tuple[str, ...] = (),
    future: bool = False,
    price: bool = True,
    settlement: bool = True,
    hash_ok: bool = True,
    topology: bool = True,
) -> DatasetObservation:
    return DatasetObservation(
        date=day,
        city=city,
        station="LFPG" if city == "paris" else "EGLC",
        checkpoint=checkpoint,
        event_family_id=family,
        month=day[:7],
        ecmwf_run_cycle="2026-03-01T00:00",
        observed=observed,
        usable=usable,
        has_settlement=settlement,
        scored=settlement,
        has_price_history=price,
        future_leakage=future,
        retrospective_substitution=False,
        raw_hash_ok=hash_ok,
        topology_valid=topology,
        topology_reviewed_quarantine=False,
        missing_reasons=missing,
    )


def _grid(*families: tuple[str, str, str]) -> tuple[list[ExpectedCell], list[DatasetObservation]]:
    expected: list[ExpectedCell] = []
    observations: list[DatasetObservation] = []
    for city, day, family in families:
        station = "LFPG" if city == "paris" else "EGLC"
        for lead in CHECKPOINTS:
            expected.append(
                ExpectedCell(
                    date=day,
                    city=city,
                    station=station,
                    checkpoint=lead,
                    event_family_id=family,
                    month=day[:7],
                    ecmwf_run_cycle="2026-03-01T00:00",
                )
            )
            observations.append(_obs(day=day, city=city, checkpoint=lead, family=family))
    return expected, observations


def test_missing_checkpoint_preserved_in_denominators() -> None:
    expected, observations = _grid(("paris", "2026-03-01", "fam-paris"))
    observations = [
        row
        if row.checkpoint != 12
        else _obs(
            day="2026-03-01",
            city="paris",
            checkpoint=12,
            family="fam-paris",
            observed=False,
            usable=False,
            missing=("missing_checkpoint",),
            price=False,
        )
        for row in observations
    ]
    audit = audit_dataset(expected=expected, observations=observations)
    overall_expected = sum(cell.expected_count for cell in audit.matrices["CHECKPOINT"])
    assert overall_expected == len(expected)
    twelve = next(cell for cell in audit.matrices["CHECKPOINT"] if cell.key == "12")
    assert twelve.expected_count == 1
    assert twelve.missing_count == 1
    assert twelve.missing_reasons["missing_checkpoint"] == 1
    assert twelve.missing_fraction == 1.0


def test_deterministic_missingness_denominators_do_not_drop_missing() -> None:
    expected, observations = _grid(
        ("paris", "2026-03-01", "fam-paris"),
        ("london", "2026-03-01", "fam-london"),
    )
    observations[0] = _obs(
        day="2026-03-01",
        city="paris",
        checkpoint=48,
        family="fam-paris",
        observed=False,
        usable=False,
        missing=("TIMEOUT",),
        price=True,
    )
    audit = audit_dataset(expected=expected, observations=observations)
    date_cell = audit.matrices["DATE"][0]
    assert date_cell.expected_count == 12
    assert date_cell.observed_count == 11
    assert date_cell.missing_count == 1
    assert date_cell.missing_fraction == pytest.approx(1 / 12)
    assert date_cell.expected_count == date_cell.observed_count + date_cell.missing_count


def test_future_leakage_blocks_dataset_ready() -> None:
    expected, observations = _grid(("paris", "2026-03-01", "fam-paris"))
    observations[0] = _obs(
        day="2026-03-01",
        city="paris",
        checkpoint=48,
        family="fam-paris",
        usable=False,
        future=True,
        missing=("forecast_future_only",),
    )
    audit = audit_dataset(expected=expected, observations=observations)
    assert audit.future_leakage_count == 1
    assert audit.phase35_dataset_ready is False
    assert (
        build_dataset_freeze(
            audit,
            collection_id="c",
            code_commit="x",
            manifest_sha256="m",
            raw_index_sha256="r",
            canonical_dataset_sha256="d",
            report_sha256="p",
            date_range={"start": START_DATE, "end": END_DATE},
            event_count=1,
            snapshot_count=1,
            checkpoint_counts={},
            city_counts={},
            missingness_summary={},
            quarantine_summary={},
        )
        is None
    )


def test_passing_audit_can_freeze_without_collecting() -> None:
    expected, observations = _grid(
        ("paris", "2026-03-01", "fam-paris"),
        ("london", "2026-03-01", "fam-london"),
        ("paris", "2026-04-01", "fam-paris-apr"),
        ("london", "2026-04-01", "fam-london-apr"),
    )
    audit = audit_dataset(expected=expected, observations=observations)
    assert audit.phase35_dataset_ready is True
    freeze = build_dataset_freeze(
        audit,
        collection_id="c1",
        code_commit="x",
        manifest_sha256="m",
        raw_index_sha256="r",
        canonical_dataset_sha256="d",
        report_sha256="p",
        date_range={"start": START_DATE, "end": END_DATE},
        event_count=4,
        snapshot_count=24,
        checkpoint_counts={"48": 4},
        city_counts={"paris": 2, "london": 2},
        missingness_summary={"missing_count": 0},
        quarantine_summary={"count": 0},
    )
    assert freeze is not None
    assert freeze.as_dict()["DATASET_ID"] == "phase35-dataset-c1"
    assert freeze.as_dict()["KNOWN_LIMITATIONS"]["HISTORICAL_CLOB_MODE"] == "descriptive_only"


def test_stable_provenance_rejects_machine_roots(tmp_path: Path) -> None:
    persisted = persist_raw_atomically(
        tmp_path,
        provider="open_meteo_single_runs",
        day="2026-03-01",
        payload={"hourly": {"temperature_2m": [1]}},
    )
    stable = stable_historical_raw_provenance_path(persisted.runtime_path)
    assert stable.startswith("historical/raw/")
    assert persisted.as_dict()["stable_path"] == stable
    for leak in ("/tmp/", "/Users/", "/home/"):
        assert leak not in stable
        assert leak not in json.dumps(persisted.as_dict())
    probe = probe_raw(Path(persisted.runtime_path), persisted.content_sha256)
    assert probe.exists is True
    assert probe.hash_matches is True
    with pytest.raises(ValueError):
        stable_historical_raw_provenance_path("/tmp/unrelated.json")


def test_readonly_http_boundary_remains_get_only() -> None:
    client = ReadOnlyHttpClient(transport=RecordingGetTransport({}), max_retries=0)
    with pytest.raises(ReadOnlyHttpError, match="GET-only"):
        client.request("POST", "https://clob.polymarket.com/prices-history")


def test_cli_plan_and_collect_refuse_without_provider_calls(tmp_path: Path) -> None:
    runner = CliRunner()
    manifest = tmp_path / "plan.json"
    plan = runner.invoke(
        main,
        [
            "phase35-full-collection-plan",
            "--start-date",
            START_DATE,
            "--end-date",
            END_DATE,
            "--manifest",
            str(manifest),
        ],
    )
    assert plan.exit_code == 0
    assert YES_PENDING_FINAL_REVIEW in plan.output
    assert "FULL_COLLECTION_START_ALLOWED" in plan.output
    assert "NOT_AUTHORIZED" in plan.output
    assert "5909" in plan.output
    assert "10858" in plan.output
    assert "11818" in plan.output
    assert REQUEST_BUDGET_REDESIGN_REQUIRED not in plan.output
    assert "network_authorized" in plan.output
    collect = runner.invoke(
        main,
        [
            "phase35-collect-historical",
            "--manifest",
            str(manifest),
            "--output-root",
            str(tmp_path / "out"),
        ],
    )
    assert collect.exit_code == 2
    assert YES_PENDING_FINAL_REVIEW in collect.output
    assert "collection_started" in collect.output
    assert "false" in collect.output.lower()
    assert "NOT_AUTHORIZED" in collect.output
    accept = runner.invoke(
        main,
        [
            "phase35-dataset-acceptance",
            "--manifest",
            str(manifest),
            "--output-root",
            str(tmp_path / "audit"),
        ],
    )
    assert accept.exit_code == 2
    assert (tmp_path / "audit" / "reports" / "phase35_dataset_acceptance.json").is_file()
    report = json.loads(
        (tmp_path / "audit" / "reports" / "phase35_dataset_acceptance.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["inferences"]["PHASE35_DATASET_READY"] is False
    assert report["inferences"]["collection_not_executed"] is True
    for leak in ("/tmp/", "/Users/", "/home/"):
        assert leak not in plan.output
        assert leak not in (
            tmp_path / "audit" / "reports" / "phase35_dataset_acceptance.json"
        ).read_text(encoding="utf-8")


def test_refuse_collection_helper_uses_budget_status() -> None:
    payload = refuse_historical_collection()
    assert payload["status"] == YES_PENDING_FINAL_REVIEW
    assert payload["FULL_COLLECTION_START_ALLOWED"] == YES_PENDING_FINAL_REVIEW
    assert payload["collection_started"] is False
    assert payload["network_authorized"] is False
    assert payload["theoretical_envelope_authorized"] is False
    assert payload["theoretical_envelope"] == "NOT_AUTHORIZED"


def test_offline_acceptance_without_observations_is_not_ready(tmp_path: Path) -> None:
    audit = run_offline_dataset_acceptance(output_dir=tmp_path)
    assert audit.phase35_dataset_ready is False
    assert not (tmp_path / "reports" / "phase35_dataset_freeze.json").exists()


def test_plan_validation_is_offline(tmp_path: Path) -> None:
    result = validate_full_collection_plan(
        manifest_path=tmp_path / "plan.json",
        code_commit="test",
        created_at=_fixed_clock(),
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES),
    )
    assert result.collection_started is False
    assert result.network_authorized is False
    assert result.status == PREFLIGHT_OK
    assert result.full_collection_start_allowed == YES_PENDING_FINAL_REVIEW
    assert result.theoretical_envelope_authorized is False
    report = result.as_dict()
    assert report["FULL_COLLECTION_START_ALLOWED"] == YES_PENDING_FINAL_REVIEW
    assert report["REQUEST_BUDGET"]["initial"]["total"] == 5909
    assert report["REQUEST_BUDGET"]["cap_bounded_maxima"]["global"] == 10858
    assert report["REQUEST_BUDGET"]["theoretical_all_transient_once"]["global"] == 11818
    assert report["REQUEST_BUDGET"]["theoretical_envelope"] == "NOT_AUTHORIZED"
