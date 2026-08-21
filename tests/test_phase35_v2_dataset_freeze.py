"""V2-aware production freeze adapter tests. Temp fixtures only; no real freeze.

Does not contact providers, mutate historical correction artifacts, or execute
freeze against real correction paths. Failure-only: real unmocked
offline_v2_corpus_audit; no fabricated readiness / success freeze writes.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from weather_alpha.cli import main
from weather_alpha.phase35.full_collection.clob_contract import canonical_clob_identity
from weather_alpha.phase35.full_collection.correction_recovery import (
    CorrectionOverlayService,
    create_correction_authorization_receipt,
    create_correction_recovery_manifest,
)
from weather_alpha.phase35.full_collection.freeze import (
    DatasetFreezeStatus,
    build_production_v2_dataset_freeze,
)
from weather_alpha.phase35.full_collection.manifest import payload_sha256
from weather_alpha.phase35.full_collection.v2_protocol import offline_v2_corpus_audit

REAL_CORRECTION = Path("data/phase35/historical/corrections/phase35-clob-correction-738f3d48dd0f")
CORRECTION_COMMIT = "v2-freeze-test-commit-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CORRECTED_AUDIT_VIEW_NAME = "corrected_audit_view"
V2_FREEZE_NAME = "phase35_v2_dataset_freeze.json"


def _fingerprint(root: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        rows[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ledger_line(
    *,
    identity: str,
    market: str,
    start_ts: int,
    end_ts: int,
    classification: str,
    collection_id: str,
) -> str:
    row = {
        "attempt_number": 1,
        "attempt_timestamp_utc": "2026-05-19T12:00:00+00:00",
        "canonical_request_identity": identity,
        "collection_id": collection_id,
        "collection_status": None,
        "content_sha256": None,
        "endpoint": "https://clob.polymarket.com/prices-history",
        "error_class": None,
        "error_detail": None,
        "hash_algorithm": "sha256",
        "http_method": "GET",
        "http_status": 200 if classification == "SUCCESS" else 400,
        "latency_ms": 1.0,
        "normalized_request_parameters": {
            "fidelity": 60,
            "market": market,
            "startTs": start_ts,
            "endTs": end_ts,
        },
        "parser_schema_version": "phase35-full-collection-parser-v1",
        "provider": "polymarket_clob",
        "result_classification": classification,
        "retry_after_seconds": None,
        "stable_raw_provenance_path": None,
    }
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def _write_synthetic_first_recovery(root: Path) -> Path:
    start_ts = 1_748_000_000
    end_ts = 1_748_100_000
    wrong_token = "tok-wrong"
    correct_token = "tok-family"
    wrong_identity = canonical_clob_identity(
        market=wrong_token, start_ts=start_ts, end_ts=end_ts, fidelity=60
    )
    namespace = root / "synthetic-first-recovery"
    (namespace / "events").mkdir(parents=True)
    (namespace / "plans").mkdir(parents=True)
    (namespace / "parsed").mkdir(parents=True)
    families = [
        {
            "event_family_id": "fam-a",
            "date": "2026-05-19",
            "city": "london",
            "station": "EGLC",
            "timezone_name": "UTC",
            "yes_token_ids": [correct_token],
            "has_settlement": True,
        }
    ]
    _write_json(namespace / "events" / "accepted.json", families)
    _write_json(namespace / "expected_cells.json", [])
    cell_map = {wrong_identity: [{"event_family_id": "fam-a", "checkpoint": 24, "city": "london"}]}
    _write_json(namespace / "plans" / "clob_cell_map.json", cell_map)
    parsed = [
        {
            "identity": wrong_identity,
            "params": {"market": "tok-parsed-lie", "startTs": 1, "endTs": 2, "fidelity": 60},
            "points": [{"observed_at": "2026-05-18T12:00:00+00:00", "price": 0.4}],
            "classification": "SUCCESS",
        }
    ]
    _write_json(namespace / "parsed" / "clob.json", parsed)
    (namespace / "ledger.jsonl").write_text(
        _ledger_line(
            identity=wrong_identity,
            market=wrong_token,
            start_ts=start_ts,
            end_ts=end_ts,
            classification="SUCCESS",
            collection_id="synthetic-first-recovery",
        )
        + "\n",
        encoding="utf-8",
    )
    return namespace


def _seed_correction_overlay(
    namespace: Path,
    *,
    corrected_identity: str,
    family_id: str,
    token: str,
    start_ts: int,
    end_ts: int,
) -> None:
    (namespace / "plans").mkdir(parents=True)
    (namespace / "parsed").mkdir(parents=True)
    cell_map = {
        corrected_identity: [{"event_family_id": family_id, "checkpoint": 24, "city": "london"}]
    }
    _write_json(namespace / "plans" / "clob_cell_map.json", cell_map)
    parsed = [
        {
            "identity": corrected_identity,
            "params": {
                "market": token,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": 60,
            },
            "points": [{"observed_at": "2026-05-18T10:00:00+00:00", "price": 0.55}],
            "classification": "SUCCESS",
        }
    ]
    _write_json(namespace / "parsed" / "clob.json", parsed)
    (namespace / "ledger.jsonl").write_text(
        _ledger_line(
            identity=corrected_identity,
            market=token,
            start_ts=start_ts,
            end_ts=end_ts,
            classification="SUCCESS",
            collection_id=namespace.name,
        )
        + "\n",
        encoding="utf-8",
    )


def _build_complete_correction_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """Temp correction namespace + corrected audit view (real NOT_YET audit)."""

    first = _write_synthetic_first_recovery(tmp_path)
    start_ts = 1_748_000_000
    end_ts = 1_748_100_000
    correct_token = "tok-family"
    correct_identity = canonical_clob_identity(
        market=correct_token, start_ts=start_ts, end_ts=end_ts, fidelity=60
    )
    manifest_path = tmp_path / "correction-manifest.json"
    created = create_correction_recovery_manifest(
        destination=manifest_path,
        first_recovery_namespace=first,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    assert created.correction_id is not None
    receipt_path = tmp_path / "correction-authorization.json"
    create_correction_authorization_receipt(
        manifest_path=manifest_path,
        destination=receipt_path,
        expected_code_commit=CORRECTION_COMMIT,
        authorized_at=datetime(2026, 8, 20, 12, 1, tzinfo=UTC),
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    correction_id = created.correction_id
    correction_ns = tmp_path / "corrections" / correction_id
    correction_ns.mkdir(parents=True)
    manifest_dest = correction_ns / "correction-manifest.json"
    receipt_dest = correction_ns / "correction-authorization.json"
    manifest_dest.write_bytes(manifest_path.read_bytes())
    receipt_dest.write_bytes(receipt_path.read_bytes())

    _seed_correction_overlay(
        correction_ns,
        corrected_identity=correct_identity,
        family_id="fam-a",
        token=correct_token,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    view = correction_ns / CORRECTED_AUDIT_VIEW_NAME
    CorrectionOverlayService(
        first_recovery_namespace=first,
        correction_namespace=correction_ns,
    ).materialize_corrected_audit_view(destination=view)

    lineage = {
        "CORRECTION_CODE_COMMIT": CORRECTION_COMMIT,
        "CORRECTION_ID": correction_id,
        "CORRECTION_MANIFEST_SHA256": created.manifest_sha256,
        "CORRECTION_NAMESPACE": str(receipt["CORRECTION_NAMESPACE"]),
        "CORRECTION_SCOPE": "CLOB_CORRECTION_V2",
        "FIRST_RECOVERY_COLLECTION_ID": "synthetic-first-recovery",
        "REQUEST_POLICY_VERSION": "phase35-full-collection-request-policy-v2",
        "V2_CORRECTION_AUDIT_SOURCE": str(receipt["V2_CORRECTION_AUDIT_SOURCE"]),
    }
    _write_json(correction_ns / "correction_lineage.json", lineage)
    progress = {
        "collection_id": correction_id,
        "correction_code_commit": CORRECTION_COMMIT,
        "correction_id": correction_id,
        "correction_namespace": lineage["CORRECTION_NAMESPACE"],
        "first_recovery_collection_id": lineage["FIRST_RECOVERY_COLLECTION_ID"],
        "interrupt_reason": None,
        "manifest_sha256": created.manifest_sha256,
        "stage": "COMPLETE",
        "terminal": True,
        "updated_at": "2026-08-20T12:02:00+00:00",
        "v2_correction_audit_source": lineage["V2_CORRECTION_AUDIT_SOURCE"],
    }
    _write_json(correction_ns / "progress.json", progress)

    audit = offline_v2_corpus_audit(view)
    _write_json(correction_ns / "reports" / "phase35_v2_audit.json", audit)

    hashes = {
        "manifest": created.manifest_sha256 or "",
        "manifest_file": _file_sha256(manifest_dest),
        "receipt": payload_sha256(receipt),
        "receipt_file": _file_sha256(receipt_dest),
        "lineage": payload_sha256(lineage),
        "lineage_file": _file_sha256(correction_ns / "correction_lineage.json"),
        "audit_canonical": _canonical_sha256(audit),
        "audit_file": _file_sha256(correction_ns / "reports" / "phase35_v2_audit.json"),
        "wrong_evidence_file": _file_sha256(
            view / "correction_overlay" / "wrong_evidence_preserved.json"
        ),
        "ledger_file": _file_sha256(correction_ns / "ledger.jsonl"),
    }
    return correction_ns, view, hashes


def test_no_fabricated_readiness_helpers_or_audit_mocks_in_module() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_affirmative_v2_audit" not in defined
    assert "_seed_generic_ready_corpus" not in defined
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert "unittest.mock" not in imported_modules
    patched_targets: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_patch = (isinstance(func, ast.Name) and func.id == "patch") or (
            isinstance(func, ast.Attribute) and func.attr == "patch"
        )
        if not is_patch:
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            patched_targets.append(node.args[0].value)
    assert not any("offline_v2_corpus_audit" in target for target in patched_targets)


def test_matching_not_yet_established_v2_audit_refuses(tmp_path: Path) -> None:
    before_real = _fingerprint(REAL_CORRECTION) if REAL_CORRECTION.is_dir() else {}
    correction_ns, view, _hashes = _build_complete_correction_fixture(tmp_path)
    audit = json.loads(
        (correction_ns / "reports" / "phase35_v2_audit.json").read_text(encoding="utf-8")
    )
    assert audit["PHASE35B_V2_DATASET_READY"] == "NOT_YET_ESTABLISHED"
    assert _canonical_sha256(audit) == _canonical_sha256(offline_v2_corpus_audit(view))

    result = build_production_v2_dataset_freeze(correction_namespace=correction_ns)
    assert result.status is DatasetFreezeStatus.REFUSED
    assert result.dataset_freeze_created is False
    assert result.reason == "v2_dataset_not_ready"
    assert not (correction_ns / "reports" / V2_FREEZE_NAME).exists()
    if REAL_CORRECTION.is_dir():
        assert _fingerprint(REAL_CORRECTION) == before_real


def test_v2_adapter_rejects_caller_selected_corrected_audit_view_param() -> None:
    params = inspect.signature(build_production_v2_dataset_freeze).parameters
    assert "corrected_audit_view" not in params
    assert "correction_namespace" in params


def test_external_corrected_audit_view_cannot_redirect_inspection(tmp_path: Path) -> None:
    correction_ns, view, _hashes = _build_complete_correction_fixture(tmp_path)
    external = tmp_path / "external_unrelated_view"
    external.mkdir(parents=True)
    (external / "correction_overlay").mkdir(parents=True)
    (external / "correction_overlay" / "wrong_evidence_preserved.json").write_text(
        '{"overlay_identities": ["external-only"]}\n',
        encoding="utf-8",
    )
    (external / "correction_overlay" / "original_parsed_clob_snapshot.json").write_text(
        "{}\n",
        encoding="utf-8",
    )

    # Remove lineage-bound derived view; external decoy must not be usable.
    for path in sorted(view.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    view.rmdir()
    assert not view.exists()
    assert external.is_dir()

    result = build_production_v2_dataset_freeze(correction_namespace=correction_ns)
    assert result.status is DatasetFreezeStatus.REFUSED
    assert result.dataset_freeze_created is False
    assert result.reason == "missing_corrected_view"
    assert not (correction_ns / "reports" / V2_FREEZE_NAME).exists()


def test_tampered_correction_lineage_fails_closed(tmp_path: Path) -> None:
    correction_ns, _view, _hashes = _build_complete_correction_fixture(tmp_path)
    lineage_path = correction_ns / "correction_lineage.json"
    lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    lineage["CORRECTION_CODE_COMMIT"] = "tampered-commit-" + ("c" * 40)
    _write_json(lineage_path, lineage)

    result = build_production_v2_dataset_freeze(correction_namespace=correction_ns)
    assert result.status is DatasetFreezeStatus.REFUSED
    assert result.dataset_freeze_created is False
    assert result.reason in {"lineage_mismatch", "provenance_mismatch"}
    assert not (correction_ns / "reports" / V2_FREEZE_NAME).exists()


def test_tampered_v2_audit_fails_closed(tmp_path: Path) -> None:
    correction_ns, view, _hashes = _build_complete_correction_fixture(tmp_path)
    audit_path = correction_ns / "reports" / "phase35_v2_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["UNRESOLVED_CORRECTION_CLOB_IDENTITY_COUNT"] = 99
    _write_json(audit_path, audit)
    assert _canonical_sha256(audit) != _canonical_sha256(offline_v2_corpus_audit(view))

    result = build_production_v2_dataset_freeze(correction_namespace=correction_ns)
    assert result.status is DatasetFreezeStatus.REFUSED
    assert result.dataset_freeze_created is False
    assert result.reason == "v2_audit_mismatch"
    assert not (correction_ns / "reports" / V2_FREEZE_NAME).exists()


def test_v2_freeze_makes_zero_network_calls_on_not_ready_refuse(tmp_path: Path) -> None:
    correction_ns, _view, _hashes = _build_complete_correction_fixture(tmp_path)
    result = build_production_v2_dataset_freeze(correction_namespace=correction_ns)
    assert result.status is DatasetFreezeStatus.REFUSED
    assert result.dataset_freeze_created is False
    assert not (correction_ns / "reports" / V2_FREEZE_NAME).exists()
    import weather_alpha.phase35.full_collection.freeze as freeze_mod

    source = Path(freeze_mod.__file__).read_text(encoding="utf-8")
    assert "httpx" not in source
    assert "requests" not in source
    assert "urllib" not in source
    assert "ReadOnlyHttpClient" not in source
    assert "_authoritative_v2_audit_for_freeze" not in source
    assert "v2_readiness_state" not in source


def test_build_production_v2_dataset_freeze_has_no_readiness_override_params() -> None:
    params = inspect.signature(build_production_v2_dataset_freeze).parameters
    for forbidden in (
        "allow",
        "force",
        "override_ready",
        "dataset_ready",
        "v2_ready",
        "skip_readiness",
        "force_freeze",
        "corrected_audit_view",
    ):
        assert forbidden not in params


def test_cli_phase35_freeze_dataset_routes_v2_and_refuses_not_ready(tmp_path: Path) -> None:
    before_real = _fingerprint(REAL_CORRECTION) if REAL_CORRECTION.is_dir() else {}
    correction_ns, _view, _hashes = _build_complete_correction_fixture(tmp_path)
    collection_root = correction_ns.parent
    collection_id = correction_ns.name
    runner = CliRunner()
    invoked = runner.invoke(
        main,
        [
            "phase35-freeze-dataset",
            "--collection-id",
            collection_id,
            "--collection-root",
            str(collection_root),
        ],
    )
    assert invoked.exit_code == 2, invoked.output
    json_start = invoked.output.find("{")
    assert json_start >= 0, invoked.output
    payload = json.loads(invoked.output[json_start:])
    assert payload.get("DATASET_FREEZE_CREATED") == "NO"
    assert payload.get("status") == "REFUSED"
    assert payload.get("reason") == "v2_dataset_not_ready"
    assert payload.get("CORRECTION_ID") == collection_id
    assert "DATASET_ID" not in payload
    assert not (correction_ns / "reports" / V2_FREEZE_NAME).exists()
    assert not (correction_ns / "reports" / "phase35_dataset_freeze.json").exists()
    if REAL_CORRECTION.is_dir():
        assert _fingerprint(REAL_CORRECTION) == before_real
