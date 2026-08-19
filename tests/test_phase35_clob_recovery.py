"""CLOB-only recovery overlay tests. Fake GET transports and temp roots only.

Does not contact providers, create production recovery manifests/receipts, or write git.
Parent collection artifacts under data/phase35/historical/ are read-only when present.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests.fakes import RecordingGetTransport
from tests.test_phase35_full_collection_orchestrator import (
    PARIS_DAY,
    _http,
    _pre_decision_history,
    _production_routes,
    _service,
)
from weather_alpha.cli import main
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyResponse
from weather_alpha.phase35.full_collection.audit import (
    audit_dataset,
    build_dataset_audit_reports,
)
from weather_alpha.phase35.full_collection.budget import StaticDiskProbe
from weather_alpha.phase35.full_collection.clob_contract import (
    canonical_clob_identity,
    clob_window_timestamps,
)
from weather_alpha.phase35.full_collection.corpus import FullCollectionCorpusAssembler
from weather_alpha.phase35.full_collection.freeze import (
    DatasetFreezeStatus,
    build_production_dataset_freeze,
)
from weather_alpha.phase35.full_collection.ledger import AppendOnlyLedger, ResultClassification
from weather_alpha.phase35.full_collection.orchestrator import CollectionStage
from weather_alpha.phase35.full_collection.policy import (
    CLOB_ATTEMPT_CAP,
    CLOB_FIDELITY_MINUTES,
    HTTP_FAILURE_BODY_PERSISTENCE_DEFERRED,
    PARENT_CLOB_HTTP_FAILURE_SCALE,
    RECOVERY_SCOPE_CLOB_ONLY,
    REQUEST_POLICY_VERSION,
    STORAGE_PREFLIGHT_MIN_BYTES,
)
from weather_alpha.phase35.full_collection.recovery import (
    ClobRecoveryService,
    RecoveryAuthorizationError,
    create_clob_recovery_authorization_receipt,
    create_clob_recovery_manifest,
    derive_clob_recovery_targets,
    load_authorized_recovery_manifest,
    merge_parent_and_recovery,
)
from weather_alpha.research.reports import write_report_pair

REAL_PARENT_ID = "phase35-hist-2026-03-01-2026-05-29-56d17239452a"
REAL_PARENT = Path("data/phase35/historical/collections") / REAL_PARENT_ID
RECOVERY_COMMIT = "recovery-test-commit"


def _fingerprint(root: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        rows[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return rows


def _clock() -> datetime:
    return datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _rewrite_parent_clob_as_old_contract_http_failure(namespace: Path) -> None:
    plans = json.loads((namespace / "plans" / "clob.json").read_text(encoding="utf-8"))
    cell_map = json.loads((namespace / "plans" / "clob_cell_map.json").read_text(encoding="utf-8"))
    old_plans: list[dict[str, Any]] = []
    old_map: dict[str, list[dict[str, Any]]] = {}
    for planned in plans:
        cells = cell_map.get(planned["identity"]) or []
        city = str(cells[0]["city"]) if cells else "paris"
        day = str(planned["day"])
        old_identity = f"clob:{city}:{day}"
        market = planned["params"]["market"]
        old_plans.append(
            {
                "day": day,
                "endpoint": planned["endpoint"],
                "identity": old_identity,
                "params": {"fidelity": 60, "market": market},
                "provider": planned["provider"],
            }
        )
        old_map[old_identity] = cells
    (namespace / "plans" / "clob.json").write_text(
        json.dumps(old_plans, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (namespace / "plans" / "clob_cell_map.json").write_text(
        json.dumps(old_map, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    parsed = []
    for planned in old_plans:
        parsed.append(
            {
                "classification": ResultClassification.HTTP_FAILURE.value,
                "content_sha256": None,
                "identity": planned["identity"],
                "kind": "clob",
                "params": dict(planned["params"]),
                "points": [],
                "price_semantics": "DESCRIPTIVE_ONLY",
                "price_selection_rule": "price.observed_at <= decision_ts; descriptive_only",
                "schema_status": None,
                "skipped": False,
                "stable_raw_provenance_path": None,
            }
        )
    (namespace / "parsed" / "clob.json").write_text(
        json.dumps(parsed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    observations = json.loads((namespace / "observations.json").read_text(encoding="utf-8"))
    for row in observations:
        row["has_price_history"] = False
        reasons = [
            item for item in row.get("missing_reasons") or [] if item != "PRICE_HISTORY_EMPTY"
        ]
        if ResultClassification.HTTP_FAILURE.value not in reasons:
            reasons.append(ResultClassification.HTTP_FAILURE.value)
        row["missing_reasons"] = reasons
    (namespace / "observations.json").write_text(
        json.dumps(observations, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    pit_path = namespace / "selections" / "pit.json"
    if pit_path.is_file():
        pit_path.write_text(
            json.dumps(observations, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    ledger_path = namespace / "ledger.jsonl"
    kept: list[str] = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        identity = str(row.get("canonical_request_identity") or "")
        if identity.startswith("clob:"):
            continue
        kept.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
    for planned in old_plans:
        pending = {
            "attempt_number": 1,
            "attempt_timestamp_utc": _clock().isoformat(),
            "canonical_request_identity": planned["identity"],
            "collection_id": json.loads((namespace / "progress.json").read_text(encoding="utf-8"))[
                "collection_id"
            ],
            "collection_status": None,
            "content_sha256": None,
            "endpoint": planned["endpoint"],
            "error_class": None,
            "error_detail": None,
            "hash_algorithm": "sha256",
            "http_method": "GET",
            "http_status": None,
            "latency_ms": None,
            "normalized_request_parameters": dict(planned["params"]),
            "parser_schema_version": "phase35-full-collection-parser-v1",
            "provider": planned["provider"],
            "result_classification": ResultClassification.PENDING.value,
            "retry_after_seconds": None,
            "stable_raw_provenance_path": None,
        }
        terminal = dict(pending)
        terminal["http_status"] = 400
        terminal["latency_ms"] = 1.0
        terminal["result_classification"] = ResultClassification.HTTP_FAILURE.value
        kept.append(json.dumps(pending, sort_keys=True, separators=(",", ":")))
        kept.append(json.dumps(terminal, sort_keys=True, separators=(",", ":")))
    ledger_path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _old_contract_parent(tmp_path: Path) -> tuple[str, Path, Path]:
    transport = RecordingGetTransport(_production_routes())
    result = _service(tmp_path, transport).run()
    assert result.stage is CollectionStage.COMPLETE
    namespace = tmp_path / "collections" / result.collection_id
    _rewrite_parent_clob_as_old_contract_http_failure(namespace)
    parent_manifest = tmp_path / "auth" / "manifests" / "authorized.json"
    return result.collection_id, namespace, parent_manifest


def _recovery_service(
    tmp_path: Path,
    transport: RecordingGetTransport,
    *,
    recovery_manifest: Path,
    authorization: Path,
    parent_root: Path,
) -> ClobRecoveryService:
    return ClobRecoveryService(
        manifest_path=recovery_manifest,
        authorization_path=authorization,
        recovery_root=tmp_path / "recoveries",
        parent_collection_root=parent_root,
        http=_http(transport),
        expected_code_commit=RECOVERY_COMMIT,
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
        sleeper=lambda _seconds: None,
        clock=_clock,
    )


def _plan_and_authorize(
    tmp_path: Path,
    *,
    parent_id: str,
    parent_root: Path,
    parent_manifest: Path,
) -> tuple[Path, Path]:
    dest = tmp_path / "recovery-manifest.json"
    created = create_clob_recovery_manifest(
        destination=dest,
        parent_collection_root=parent_root,
        parent_collection_id=parent_id,
        parent_manifest_path=parent_manifest,
        code_commit=RECOVERY_COMMIT,
        created_at=_clock(),
    )
    assert created.written is True
    receipt = tmp_path / "recovery-authorization.json"
    create_clob_recovery_authorization_receipt(
        manifest_path=dest,
        destination=receipt,
        expected_code_commit=RECOVERY_COMMIT,
        authorized_at=_clock(),
    )
    return dest, receipt


def test_recovery_derives_clob_targets_from_parent_ledger(tmp_path: Path) -> None:
    parent_id, namespace, _manifest = _old_contract_parent(tmp_path)
    derived = derive_clob_recovery_targets(namespace)
    assert parent_id
    assert derived.gamma_recovery_identities == 0
    assert derived.ecmwf_recovery_identities == 0
    assert len(derived.targets) == 1
    target = derived.targets[0]
    assert target.city == "paris"
    assert target.date == PARIS_DAY
    assert target.token
    assert target.fidelity == CLOB_FIDELITY_MINUTES
    start_ts, end_ts = clob_window_timestamps(PARIS_DAY, "Europe/Paris")
    assert target.start_ts == start_ts
    assert target.end_ts == end_ts
    assert target.identity == canonical_clob_identity(
        market=target.token, start_ts=start_ts, end_ts=end_ts, fidelity=60
    )
    assert target.identity != f"clob:paris:{PARIS_DAY}"
    assert target.parent_identity == f"clob:paris:{PARIS_DAY}"


def test_recovery_does_not_accept_fabricated_token_list() -> None:
    params = inspect.signature(derive_clob_recovery_targets).parameters
    for forbidden in ("tokens", "token_ids", "token_list", "markets", "market_ids"):
        assert forbidden not in params
    params_plan = inspect.signature(create_clob_recovery_manifest).parameters
    for forbidden in ("tokens", "token_ids", "token_list", "markets"):
        assert forbidden not in params_plan


def test_recovery_gamma_and_ecmwf_identities_are_zero(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest = tmp_path / "recovery-manifest.json"
    created = create_clob_recovery_manifest(
        destination=dest,
        parent_collection_root=tmp_path / "collections",
        parent_collection_id=parent_id,
        parent_manifest_path=parent_manifest,
        code_commit=RECOVERY_COMMIT,
        created_at=_clock(),
    )
    payload = created.payload
    assert payload["GAMMA_RECOVERY_IDENTITIES"] == 0
    assert payload["ECMWF_RECOVERY_IDENTITIES"] == 0
    assert payload["RECOVERY_SCOPE"] == RECOVERY_SCOPE_CLOB_ONLY
    assert payload["CLOB_RECOVERY_IDENTITIES"] == 1
    assert payload["CLOB_REQUEST_POLICY_VERSION"] == REQUEST_POLICY_VERSION
    assert payload["REQUEST_CAPS"]["clob_attempts"] == CLOB_ATTEMPT_CAP


def test_recovery_schedules_only_corrected_clob_identities(tmp_path: Path) -> None:
    parent_id, namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest = tmp_path / "recovery-manifest.json"
    created = create_clob_recovery_manifest(
        destination=dest,
        parent_collection_root=tmp_path / "collections",
        parent_collection_id=parent_id,
        parent_manifest_path=parent_manifest,
        code_commit=RECOVERY_COMMIT,
        created_at=_clock(),
    )
    planned = created.payload["PLANNED_GETS"]
    assert planned
    for row in planned:
        params = dict(row["params"])
        assert set(params) == {"market", "startTs", "endTs", "fidelity"}
        assert row["identity"] != f"clob:paris:{PARIS_DAY}"
        assert row["provider"] == "polymarket_clob_prices_history"
    derived = derive_clob_recovery_targets(namespace)
    assert {row["identity"] for row in planned} == {target.identity for target in derived.targets}


def test_parent_files_unchanged_through_recovery(tmp_path: Path) -> None:
    parent_id, namespace, parent_manifest = _old_contract_parent(tmp_path)
    before = _fingerprint(namespace)
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    transport = RecordingGetTransport(_production_routes())
    _recovery_service(
        tmp_path,
        transport,
        recovery_manifest=dest,
        authorization=receipt,
        parent_root=tmp_path / "collections",
    ).run()
    merge_parent_and_recovery(
        parent_collection_root=tmp_path / "collections",
        parent_collection_id=parent_id,
        recovery_root=tmp_path / "recoveries",
        recovery_id=json.loads(dest.read_text(encoding="utf-8"))["RECOVERY_ID"],
    )
    assert _fingerprint(namespace) == before


def test_recovery_uses_independent_ledger_raw_namespace(tmp_path: Path) -> None:
    parent_id, namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    recovery_id = json.loads(dest.read_text(encoding="utf-8"))["RECOVERY_ID"]
    transport = RecordingGetTransport(_production_routes())
    result = _recovery_service(
        tmp_path,
        transport,
        recovery_manifest=dest,
        authorization=receipt,
        parent_root=tmp_path / "collections",
    ).run()
    recovery_ns = tmp_path / "recoveries" / recovery_id
    assert result.recovery_id == recovery_id
    assert (recovery_ns / "ledger.jsonl").is_file()
    assert recovery_ns / "ledger.jsonl" != namespace / "ledger.jsonl"
    assert list((recovery_ns / "historical" / "raw").rglob("*.json"))
    assert not (tmp_path / "recoveries" / parent_id).exists()


def test_recovery_authorization_required(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest = tmp_path / "recovery-manifest.json"
    create_clob_recovery_manifest(
        destination=dest,
        parent_collection_root=tmp_path / "collections",
        parent_collection_id=parent_id,
        parent_manifest_path=parent_manifest,
        code_commit=RECOVERY_COMMIT,
        created_at=_clock(),
    )
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(RecoveryAuthorizationError, match="missing_authorization"):
        ClobRecoveryService(
            manifest_path=dest,
            authorization_path=tmp_path / "absent-recovery-authorization.json",
            recovery_root=tmp_path / "recoveries",
            parent_collection_root=tmp_path / "collections",
            http=_http(transport),
            expected_code_commit=RECOVERY_COMMIT,
            disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
            sleeper=lambda _seconds: None,
            clock=_clock,
        ).run()
    assert transport.calls == []
    assert not (tmp_path / "recoveries").exists() or not any(
        (tmp_path / "recoveries").rglob("ledger.jsonl")
    )


def test_recovery_manifest_tamper_refuses(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    original_receipt = receipt.read_bytes()
    payload = json.loads(dest.read_text(encoding="utf-8"))
    payload["CREATED_AT"] = "2020-01-01T00:00:00+00:00"
    dest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(RecoveryAuthorizationError, match="manifest_sha_mismatch"):
        _recovery_service(
            tmp_path,
            transport,
            recovery_manifest=dest,
            authorization=receipt,
            parent_root=tmp_path / "collections",
        ).run()
    assert transport.calls == []
    assert receipt.read_bytes() == original_receipt


def test_recovery_receipt_tamper_refuses(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    original_manifest = dest.read_bytes()
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["RECOVERY_MANIFEST_SHA256"] = "0" * 64
    receipt.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(RecoveryAuthorizationError, match="manifest_sha_mismatch"):
        _recovery_service(
            tmp_path,
            transport,
            recovery_manifest=dest,
            authorization=receipt,
            parent_root=tmp_path / "collections",
        ).run()
    assert transport.calls == []
    assert dest.read_bytes() == original_manifest


def test_caller_boolean_cannot_enable_recovery_networking(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest = tmp_path / "recovery-manifest.json"
    create_clob_recovery_manifest(
        destination=dest,
        parent_collection_root=tmp_path / "collections",
        parent_collection_id=parent_id,
        parent_manifest_path=parent_manifest,
        code_commit=RECOVERY_COMMIT,
        created_at=_clock(),
    )
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(TypeError):
        ClobRecoveryService(
            manifest_path=dest,
            authorization_path=tmp_path / "absent.json",
            recovery_root=tmp_path / "recoveries",
            parent_collection_root=tmp_path / "collections",
            http=_http(transport),
            expected_code_commit=RECOVERY_COMMIT,
            network_authorized=True,  # type: ignore[call-arg]
        )
    names = inspect.signature(ClobRecoveryService.__init__).parameters
    for forbidden in ("network_authorized", "enable_network", "force_network", "allow_network"):
        assert forbidden not in names
    assert transport.calls == []


def test_recovery_restart_skips_verified_success(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    transport = RecordingGetTransport(_production_routes())
    first = _recovery_service(
        tmp_path,
        transport,
        recovery_manifest=dest,
        authorization=receipt,
        parent_root=tmp_path / "collections",
    ).run()
    clob_calls = [call for call in transport.calls if "/prices-history" in call[1]]
    assert clob_calls
    second = _recovery_service(
        tmp_path,
        transport,
        recovery_manifest=dest,
        authorization=receipt,
        parent_root=tmp_path / "collections",
    ).run()
    assert first.stage.value == "COMPLETE"
    assert second.skipped_replay is True
    assert [call for call in transport.calls if "/prices-history" in call[1]] == clob_calls


def test_corrected_200_nonempty_becomes_usable_descriptive_clob(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    recovery_id = json.loads(dest.read_text(encoding="utf-8"))["RECOVERY_ID"]
    transport = RecordingGetTransport(_production_routes(clob=_pre_decision_history()))
    _recovery_service(
        tmp_path,
        transport,
        recovery_manifest=dest,
        authorization=receipt,
        parent_root=tmp_path / "collections",
    ).run()
    merged = merge_parent_and_recovery(
        parent_collection_root=tmp_path / "collections",
        parent_collection_id=parent_id,
        recovery_root=tmp_path / "recoveries",
        recovery_id=recovery_id,
    )
    assert merged.observations
    assert all(row.has_price_history for row in merged.observations)


def test_corrected_200_empty_remains_explicit_missingness(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    recovery_id = json.loads(dest.read_text(encoding="utf-8"))["RECOVERY_ID"]
    transport = RecordingGetTransport(_production_routes(clob={"history": []}))
    _recovery_service(
        tmp_path,
        transport,
        recovery_manifest=dest,
        authorization=receipt,
        parent_root=tmp_path / "collections",
    ).run()
    merged = merge_parent_and_recovery(
        parent_collection_root=tmp_path / "collections",
        parent_collection_id=parent_id,
        recovery_root=tmp_path / "recoveries",
        recovery_id=recovery_id,
    )
    assert all(row.has_price_history is False for row in merged.observations)
    assert any("PRICE_HISTORY_EMPTY" in row.missing_reasons for row in merged.observations)


def test_deterministic_400_is_http_failure_non_retryable(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )

    class Always400:
        def get(
            self,
            url: str,
            *,
            params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> ReadOnlyResponse:
            del params, headers, timeout
            return ReadOnlyResponse(status_code=400, url=url, headers={}, content=b"{}")

    transport = Always400()
    http = ReadOnlyHttpClient(
        transport=transport, max_retries=0, retry_statuses=frozenset(), sleeper=lambda _s: None
    )
    result = ClobRecoveryService(
        manifest_path=dest,
        authorization_path=receipt,
        recovery_root=tmp_path / "recoveries",
        parent_collection_root=tmp_path / "collections",
        http=http,
        expected_code_commit=RECOVERY_COMMIT,
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
        sleeper=lambda _seconds: None,
        clock=_clock,
    ).run()
    recovery_id = json.loads(dest.read_text(encoding="utf-8"))["RECOVERY_ID"]
    ledger = AppendOnlyLedger(tmp_path / "recoveries" / recovery_id / "ledger.jsonl")
    terminal = [
        row
        for row in ledger.records()
        if row.result_classification is ResultClassification.HTTP_FAILURE
    ]
    assert terminal
    assert all(row.attempt_number == 1 for row in terminal)
    assert result.stage.value == "COMPLETE"


def test_transient_gets_at_most_one_retry(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    calls = {"n": 0}

    class OneRetryThen200:
        def get(
            self,
            url: str,
            *,
            params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> ReadOnlyResponse:
            del headers, timeout
            calls["n"] += 1
            full = url
            if "/prices-history" in url:
                if calls["n"] == 1:
                    return ReadOnlyResponse(status_code=429, url=full, headers={}, content=b"{}")
                import json as json_lib

                body = json_lib.dumps(_pre_decision_history()).encode("utf-8")
                return ReadOnlyResponse(status_code=200, url=full, headers={}, content=body)
            return ReadOnlyResponse(status_code=404, url=full, headers={}, content=b"{}")

    http = ReadOnlyHttpClient(
        transport=OneRetryThen200(),
        max_retries=0,
        retry_statuses=frozenset(),
        sleeper=lambda _s: None,
    )
    ClobRecoveryService(
        manifest_path=dest,
        authorization_path=receipt,
        recovery_root=tmp_path / "recoveries",
        parent_collection_root=tmp_path / "collections",
        http=http,
        expected_code_commit=RECOVERY_COMMIT,
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
        sleeper=lambda _seconds: None,
        clock=_clock,
    ).run()
    assert calls["n"] == 2
    recovery_id = json.loads(dest.read_text(encoding="utf-8"))["RECOVERY_ID"]
    ledger = AppendOnlyLedger(tmp_path / "recoveries" / recovery_id / "ledger.jsonl")
    clob_attempts = [
        row
        for row in ledger.records()
        if row.result_classification
        not in {
            ResultClassification.PENDING,
            ResultClassification.SKIPPED_ALREADY_COMPLETE,
        }
        and row.canonical_request_identity.startswith("clob:")
    ]
    assert len(clob_attempts) == 2
    assert clob_attempts[0].result_classification is ResultClassification.RATE_LIMITED
    assert clob_attempts[1].result_classification in {
        ResultClassification.SUCCESS,
        ResultClassification.VALID_EMPTY,
    }


def test_merged_corpus_parent_gamma_ecmwf_plus_recovery_clob(tmp_path: Path) -> None:
    parent_id, namespace, parent_manifest = _old_contract_parent(tmp_path)
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    recovery_id = json.loads(dest.read_text(encoding="utf-8"))["RECOVERY_ID"]
    transport = RecordingGetTransport(_production_routes())
    _recovery_service(
        tmp_path,
        transport,
        recovery_manifest=dest,
        authorization=receipt,
        parent_root=tmp_path / "collections",
    ).run()
    merged = merge_parent_and_recovery(
        parent_collection_root=tmp_path / "collections",
        parent_collection_id=parent_id,
        recovery_root=tmp_path / "recoveries",
        recovery_id=recovery_id,
    )
    parent_expected = json.loads((namespace / "expected_cells.json").read_text(encoding="utf-8"))
    assert [row.as_dict() for row in merged.expected] == parent_expected
    gamma_calls = [call for call in transport.calls if "/public-search" in call[1]]
    ecmwf_calls = [call for call in transport.calls if "single-runs-api" in call[1]]
    assert gamma_calls == []
    assert ecmwf_calls == []
    assert any("/prices-history" in call[1] for call in transport.calls)
    recovery_ns = tmp_path / "recoveries" / recovery_id
    assert not (recovery_ns / "parsed" / "ecmwf.json").exists()
    assert (recovery_ns / "parsed" / "clob.json").is_file()


def test_parent_400_evidence_preserved(tmp_path: Path) -> None:
    parent_id, namespace, parent_manifest = _old_contract_parent(tmp_path)
    before_ledger = (namespace / "ledger.jsonl").read_bytes()
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    transport = RecordingGetTransport(_production_routes())
    _recovery_service(
        tmp_path,
        transport,
        recovery_manifest=dest,
        authorization=receipt,
        parent_root=tmp_path / "collections",
    ).run()
    assert (namespace / "ledger.jsonl").read_bytes() == before_ledger
    ledger = AppendOnlyLedger(namespace / "ledger.jsonl")
    http_failures = [
        row
        for row in ledger.records()
        if row.result_classification is ResultClassification.HTTP_FAILURE
        and row.canonical_request_identity.startswith("clob:")
    ]
    assert http_failures
    assert all(row.http_status == 400 for row in http_failures)
    assert all("startTs" not in row.normalized_request_parameters for row in http_failures)


def test_merged_audit_recomputes_clob_coverage_and_ready(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    parent_corpus = FullCollectionCorpusAssembler(
        collection_root=tmp_path / "collections", collection_id=parent_id
    ).assemble()
    parent_audit = audit_dataset(
        expected=parent_corpus.expected, observations=parent_corpus.observations
    )
    assert parent_audit.descriptive_price_coverage_acceptable is False
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    recovery_id = json.loads(dest.read_text(encoding="utf-8"))["RECOVERY_ID"]
    transport = RecordingGetTransport(_production_routes())
    _recovery_service(
        tmp_path,
        transport,
        recovery_manifest=dest,
        authorization=receipt,
        parent_root=tmp_path / "collections",
    ).run()
    merged = merge_parent_and_recovery(
        parent_collection_root=tmp_path / "collections",
        parent_collection_id=parent_id,
        recovery_root=tmp_path / "recoveries",
        recovery_id=recovery_id,
    )
    audit = audit_dataset(expected=merged.expected, observations=merged.observations)
    reports_dir = tmp_path / "recoveries" / recovery_id / "reports"
    machine, human = build_dataset_audit_reports(audit, collection_not_executed=False)
    write_report_pair(
        reports_dir / "phase35_historical_audit.md",
        reports_dir / "phase35_historical_audit.json",
        human,
        machine,
    )
    assert audit.descriptive_price_coverage_acceptable is True
    assert audit.phase35_dataset_ready is True
    freeze = build_production_dataset_freeze(
        collection_root=tmp_path / "recoveries",
        collection_id=recovery_id,
        manifest_path=dest,
    )
    assert freeze.status is DatasetFreezeStatus.SUCCESS
    payload = json.loads(
        (
            tmp_path / "recoveries" / recovery_id / "reports" / "phase35_dataset_freeze.json"
        ).read_text(encoding="utf-8")
    )
    lineage = payload["RECOVERY_LINEAGE"]
    assert lineage["PARENT_COLLECTION_ID"] == parent_id
    assert lineage["RECOVERY_ID"] == recovery_id
    assert lineage["RECOVERY_SCOPE"] == RECOVERY_SCOPE_CLOB_ONLY


def test_http_failure_body_persistence_is_deferred() -> None:
    assert HTTP_FAILURE_BODY_PERSISTENCE_DEFERRED is True


def test_real_parent_derives_435_http_failure_clob_targets() -> None:
    ledger_path = REAL_PARENT / "ledger.jsonl"
    if not ledger_path.is_file():
        pytest.skip("preserved parent collection artifacts are not present")
    watched = (
        REAL_PARENT / "ledger.jsonl",
        REAL_PARENT / "progress.json",
        REAL_PARENT / "plans" / "clob.json",
        REAL_PARENT / "events" / "accepted.json",
        REAL_PARENT / "observations.json",
    )
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in watched}
    derived = derive_clob_recovery_targets(REAL_PARENT)
    assert derived.gamma_recovery_identities == 0
    assert derived.ecmwf_recovery_identities == 0
    assert len(derived.targets) == PARENT_CLOB_HTTP_FAILURE_SCALE
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in watched}
    assert after == before
    tokens = {target.token for target in derived.targets}
    assert tokens
    assert all(target.identity != target.parent_identity for target in derived.targets)
    assert all(target.start_ts < target.end_ts for target in derived.targets)


def test_offline_recovery_e2e_parent_to_freeze_lineage(tmp_path: Path) -> None:
    parent_id, namespace, parent_manifest = _old_contract_parent(tmp_path)
    parent_before = _fingerprint(namespace)
    dest, receipt = _plan_and_authorize(
        tmp_path,
        parent_id=parent_id,
        parent_root=tmp_path / "collections",
        parent_manifest=parent_manifest,
    )
    recovery_id = json.loads(dest.read_text(encoding="utf-8"))["RECOVERY_ID"]
    transport = RecordingGetTransport(_production_routes())
    executed = _recovery_service(
        tmp_path,
        transport,
        recovery_manifest=dest,
        authorization=receipt,
        parent_root=tmp_path / "collections",
    ).run()
    assert executed.stage.value == "COMPLETE"
    merged = merge_parent_and_recovery(
        parent_collection_root=tmp_path / "collections",
        parent_collection_id=parent_id,
        recovery_root=tmp_path / "recoveries",
        recovery_id=recovery_id,
    )
    corpus = FullCollectionCorpusAssembler(
        collection_root=tmp_path / "recoveries", collection_id=recovery_id
    ).assemble()
    assert corpus.expected == merged.expected
    audit = audit_dataset(expected=corpus.expected, observations=corpus.observations)
    reports_dir = tmp_path / "recoveries" / recovery_id / "reports"
    machine, human = build_dataset_audit_reports(audit, collection_not_executed=False)
    write_report_pair(
        reports_dir / "phase35_historical_audit.md",
        reports_dir / "phase35_historical_audit.json",
        human,
        machine,
    )
    assert audit.phase35_dataset_ready is True
    freeze = build_production_dataset_freeze(
        collection_root=tmp_path / "recoveries",
        collection_id=recovery_id,
        manifest_path=dest,
    )
    assert freeze.status is DatasetFreezeStatus.SUCCESS
    payload = json.loads(
        (
            tmp_path / "recoveries" / recovery_id / "reports" / "phase35_dataset_freeze.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["RECOVERY_LINEAGE"]["PARENT_COLLECTION_ID"] == parent_id
    assert payload["RECOVERY_LINEAGE"]["RECOVERY_ID"] == recovery_id
    assert payload["COLLECTION_ID"] == recovery_id
    assert _fingerprint(namespace) == parent_before
    gamma = [call for call in transport.calls if "/public-search" in call[1]]
    ecmwf = [call for call in transport.calls if "single-runs-api" in call[1]]
    assert gamma == []
    assert ecmwf == []
    assert (
        load_authorized_recovery_manifest(
            dest, authorization_path=receipt, expected_code_commit=RECOVERY_COMMIT
        ).network_authorized
        is True
    )


def test_cli_recovery_plan_authorize_audit_are_separate_and_offline(tmp_path: Path) -> None:
    parent_id, _namespace, parent_manifest = _old_contract_parent(tmp_path)
    runner = CliRunner()
    manifest = tmp_path / "cli-recovery-manifest.json"
    planned = runner.invoke(
        main,
        [
            "phase35-plan-clob-recovery",
            "--parent-collection-id",
            parent_id,
            "--parent-collection-root",
            str(tmp_path / "collections"),
            "--parent-manifest",
            str(parent_manifest),
            "--manifest",
            str(manifest),
            "--code-commit",
            RECOVERY_COMMIT,
        ],
    )
    assert planned.exit_code == 0, planned.output
    assert "PROVIDER_REQUESTS" in planned.output
    assert manifest.is_file()
    authorization = tmp_path / "cli-recovery-authorization.json"
    authorized = runner.invoke(
        main,
        [
            "phase35-authorize-clob-recovery",
            "--manifest",
            str(manifest),
            "--authorization",
            str(authorization),
            "--code-commit",
            RECOVERY_COMMIT,
        ],
    )
    assert authorized.exit_code == 0, authorized.output
    assert authorization.is_file()
    refused = runner.invoke(
        main,
        [
            "phase35-execute-clob-recovery",
            "--manifest",
            str(manifest),
            "--authorization",
            str(tmp_path / "missing-auth.json"),
            "--recovery-root",
            str(tmp_path / "recoveries"),
            "--parent-collection-root",
            str(tmp_path / "collections"),
            "--code-commit",
            RECOVERY_COMMIT,
        ],
    )
    assert refused.exit_code == 2
    assert "PROVIDER_REQUESTS" in refused.output
    assert "REFUSED" in refused.output
