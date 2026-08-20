"""Phase35B V2 correction-recovery path tests.

Offline only: fake local artifacts and the immutable first-recovery corpus.
Does not contact providers, authorize real recovery, or mutate historical data.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.fakes import RecordingGetTransport
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyTransport
from weather_alpha.phase35.full_collection.clob_contract import canonical_clob_identity
from weather_alpha.phase35.full_collection.correction_recovery import (
    CorrectionAuthorizationError,
    CorrectionOverlayService,
    CorrectionRecoveryService,
    create_correction_authorization_receipt,
    create_correction_recovery_manifest,
    derive_v2_correction_targets,
    load_authorized_correction_manifest,
)
from weather_alpha.phase35.full_collection.orchestrator import CollectionStage
from weather_alpha.phase35.full_collection.policy import (
    CORRECTION_RAW_STORAGE_NAMESPACE,
    CORRECTION_REASON_MISSING_CANONICAL_FAMILY_OWNED_HISTORY,
    FIRST_RECOVERY_COLLECTION_ID,
    V2_CORRECTION_PROVENANCE_COUNT,
    V2_CORRECTION_TARGET_COUNT,
)
from weather_alpha.phase35.full_collection.v2_protocol import offline_v2_corpus_audit

REAL_FIRST_RECOVERY = Path("data/phase35/historical/recoveries") / FIRST_RECOVERY_COLLECTION_ID
REAL_CORRECTION = Path("data/phase35/historical/corrections/phase35-clob-correction-8d49dedc30d6")
CORRECTION_COMMIT = "correction-test-commit-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _clock() -> datetime:
    return datetime(2026, 8, 20, 16, 0, tzinfo=UTC)


def _http(transport: ReadOnlyTransport) -> ReadOnlyHttpClient:
    return ReadOnlyHttpClient(
        transport=transport,
        max_retries=0,
        retry_statuses=frozenset(),
        sleeper=lambda _seconds: None,
    )


def _clob_history_payload() -> dict[str, object]:
    return {
        "history": [
            {"t": int(datetime(2026, 5, 18, 12, tzinfo=UTC).timestamp()), "p": 0.41},
        ]
    }


def _fingerprint(root: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        rows[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return rows


def _ledger_line(
    *,
    identity: str,
    market: str,
    start_ts: int,
    end_ts: int,
    classification: str,
    collection_id: str = "synthetic-first-recovery",
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
    """Cross-assigned family with ledger authoritative over lying map/parsed."""

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
    (namespace / "events" / "accepted.json").write_text(
        json.dumps(families, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (namespace / "expected_cells.json").write_text("[]\n", encoding="utf-8")
    # Map/parsed deliberately disagree with ledger market/window.
    cell_map = {wrong_identity: [{"event_family_id": "fam-a", "checkpoint": 24, "city": "london"}]}
    (namespace / "plans" / "clob_cell_map.json").write_text(
        json.dumps(cell_map, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    parsed = [
        {
            "identity": wrong_identity,
            "params": {"market": "tok-parsed-lie", "startTs": 1, "endTs": 2, "fidelity": 60},
            "points": [{"observed_at": "2026-05-18T12:00:00+00:00", "price": 0.4}],
            "classification": "SUCCESS",
        }
    ]
    (namespace / "parsed" / "clob.json").write_text(
        json.dumps(parsed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (namespace / "ledger.jsonl").write_text(
        _ledger_line(
            identity=wrong_identity,
            market=wrong_token,
            start_ts=start_ts,
            end_ts=end_ts,
            classification="SUCCESS",
        )
        + "\n",
        encoding="utf-8",
    )
    (namespace / "progress.json").write_text(
        json.dumps(
            {
                "collection_id": "synthetic-first-recovery",
                "manifest_sha256": "a" * 64,
                "stage": "COMPLETE",
                "terminal": True,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return namespace


def test_real_first_recovery_derives_exactly_five_correction_identities() -> None:
    assert REAL_FIRST_RECOVERY.is_dir()
    derivation = derive_v2_correction_targets(REAL_FIRST_RECOVERY)
    assert derivation.correction_clob_identity_count == V2_CORRECTION_TARGET_COUNT
    assert derivation.correction_gamma_identity_count == 0
    assert derivation.correction_ecmwf_identity_count == 0
    assert len(derivation.identities) == V2_CORRECTION_TARGET_COUNT
    assert len(derivation.provenance) == V2_CORRECTION_PROVENANCE_COUNT
    assert set(derivation.identities) == {
        "clob:range:0e5e70dd308314b2852f0b07b63a8be38ee83a8d09bd138e494fda05b6a34ca5",
        "clob:range:4ba45ae25dc365d0ec3198b1b90be361a9ad04edaecba25e1a9b67290d4d00a3",
        "clob:range:85b91fa405262a54698ad649c576b0881e55d97ad4fb21d9b8bd6974c0f2a789",
        "clob:range:bec243e7a820d4f77e3fe0dec77b0c841611bd9948ef7e4b11e94c4f6ea7ac9b",
        "clob:range:fae1e9afa121cb5f71ef855966406b30b8e5001637146bf425580d5f2351e4c9",
    }
    for row in derivation.provenance:
        assert row.incorrect_old_identity
        assert row.incorrect_old_token
        assert row.correct_token
        assert row.correct_token != row.incorrect_old_token
        assert row.ledger_evidence_collection_id == FIRST_RECOVERY_COLLECTION_ID
        assert row.reason == CORRECTION_REASON_MISSING_CANONICAL_FAMILY_OWNED_HISTORY


def test_ledger_wins_when_map_and_parsed_conflict(tmp_path: Path) -> None:
    namespace = _write_synthetic_first_recovery(tmp_path)
    start_ts = 1_748_000_000
    end_ts = 1_748_100_000
    correct_identity = canonical_clob_identity(
        market="tok-family", start_ts=start_ts, end_ts=end_ts, fidelity=60
    )
    wrong_identity = canonical_clob_identity(
        market="tok-wrong", start_ts=start_ts, end_ts=end_ts, fidelity=60
    )
    derivation = derive_v2_correction_targets(namespace)
    assert derivation.identities == (correct_identity,)
    assert derivation.provenance[0].incorrect_old_identity == wrong_identity
    assert derivation.provenance[0].incorrect_old_token == "tok-wrong"
    assert derivation.provenance[0].correct_token == "tok-family"
    assert derivation.provenance[0].start_ts == start_ts
    assert derivation.provenance[0].end_ts == end_ts


def test_correction_path_never_uses_legacy_435_recovery_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from weather_alpha.phase35.full_collection import correction_recovery as mod
    from weather_alpha.phase35.full_collection import recovery as legacy

    banned = MagicMock(side_effect=AssertionError("legacy 435 recovery planner must not be used"))
    monkeypatch.setattr(legacy, "derive_clob_recovery_targets", banned)
    monkeypatch.setattr(legacy, "create_clob_recovery_manifest", banned)
    if hasattr(mod, "derive_clob_recovery_targets"):
        monkeypatch.setattr(mod, "derive_clob_recovery_targets", banned)
    if hasattr(mod, "create_clob_recovery_manifest"):
        monkeypatch.setattr(mod, "create_clob_recovery_manifest", banned)

    namespace = _write_synthetic_first_recovery(tmp_path)
    derivation = derive_v2_correction_targets(namespace)
    assert derivation.correction_clob_identity_count == 1
    created = create_correction_recovery_manifest(
        destination=tmp_path / "correction-manifest.json",
        first_recovery_namespace=namespace,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    assert created.written is True
    assert created.payload["CLOB_CORRECTION_IDENTITIES"] == 1
    assert created.payload["GAMMA_CORRECTION_IDENTITIES"] == 0
    assert created.payload["ECMWF_CORRECTION_IDENTITIES"] == 0
    banned.assert_not_called()
    source = inspect.getsource(mod.derive_v2_correction_targets)
    assert "derive_clob_recovery_targets" not in source
    assert "PARENT_CLOB_HTTP_FAILURE_SCALE" not in source
    assert "create_clob_recovery_manifest" not in source


def test_manifest_proves_identity_ownership_reason_and_provenance(tmp_path: Path) -> None:
    namespace = _write_synthetic_first_recovery(tmp_path)
    dest = tmp_path / "correction-manifest.json"
    created = create_correction_recovery_manifest(
        destination=dest,
        first_recovery_namespace=namespace,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    entries = created.payload["CORRECTION_ENTRIES"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["original_requested_identity"]
    assert entry["incorrect_old_identity"] == entry["original_requested_identity"]
    assert entry["incorrect_old_token"]
    assert entry["corrected_canonical_token"]
    assert entry["corrected_identity"]
    assert entry["correction_reason"] == CORRECTION_REASON_MISSING_CANONICAL_FAMILY_OWNED_HISTORY
    assert entry["provenance_source"] == "synthetic-first-recovery"
    assert entry["ledger_evidence_collection_id"] == "synthetic-first-recovery"
    assert created.payload["FIRST_RECOVERY_COLLECTION_ID"] == "synthetic-first-recovery"
    assert created.payload["V2_CORRECTION_AUDIT_SOURCE"] == "synthetic-first-recovery"


def test_receipt_binds_namespace_manifest_identities_and_v2_source(tmp_path: Path) -> None:
    namespace = _write_synthetic_first_recovery(tmp_path)
    manifest_path = tmp_path / "correction-manifest.json"
    created = create_correction_recovery_manifest(
        destination=manifest_path,
        first_recovery_namespace=namespace,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    receipt_path = tmp_path / "correction-authorization.json"
    receipt = create_correction_authorization_receipt(
        manifest_path=manifest_path,
        destination=receipt_path,
        expected_code_commit=CORRECTION_COMMIT,
        authorized_at=datetime(2026, 8, 20, 12, 5, tzinfo=UTC),
    )
    assert receipt.correction_id == created.correction_id
    assert receipt.correction_manifest_sha256 == created.manifest_sha256
    assert tuple(receipt.correction_identities) == created.identities
    assert receipt.v2_correction_audit_source == "synthetic-first-recovery"
    assert receipt.correction_namespace.endswith(created.correction_id)
    authorized = load_authorized_correction_manifest(
        manifest_path,
        authorization_path=receipt_path,
        expected_code_commit=CORRECTION_COMMIT,
    )
    assert authorized.network_authorized is True
    assert authorized.correction_identities == created.identities


def test_manifest_tamper_refuses_authorization(tmp_path: Path) -> None:
    namespace = _write_synthetic_first_recovery(tmp_path)
    manifest_path = tmp_path / "correction-manifest.json"
    create_correction_recovery_manifest(
        destination=manifest_path,
        first_recovery_namespace=namespace,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    receipt_path = tmp_path / "correction-authorization.json"
    create_correction_authorization_receipt(
        manifest_path=manifest_path,
        destination=receipt_path,
        expected_code_commit=CORRECTION_COMMIT,
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["PLANNED_GETS"].append(payload["PLANNED_GETS"][0])
    payload["CLOB_CORRECTION_IDENTITIES"] = len(payload["PLANNED_GETS"])
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(CorrectionAuthorizationError, match="manifest_sha_mismatch"):
        load_authorized_correction_manifest(
            manifest_path,
            authorization_path=receipt_path,
            expected_code_commit=CORRECTION_COMMIT,
        )


def test_receipt_tamper_refuses_authorization(tmp_path: Path) -> None:
    namespace = _write_synthetic_first_recovery(tmp_path)
    manifest_path = tmp_path / "correction-manifest.json"
    create_correction_recovery_manifest(
        destination=manifest_path,
        first_recovery_namespace=namespace,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    receipt_path = tmp_path / "correction-authorization.json"
    create_correction_authorization_receipt(
        manifest_path=manifest_path,
        destination=receipt_path,
        expected_code_commit=CORRECTION_COMMIT,
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["CORRECTION_MANIFEST_SHA256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(CorrectionAuthorizationError, match="manifest_sha_mismatch"):
        load_authorized_correction_manifest(
            manifest_path,
            authorization_path=receipt_path,
            expected_code_commit=CORRECTION_COMMIT,
        )


def test_correction_namespace_tamper_refuses_authorization_fail_closed(tmp_path: Path) -> None:
    """CORRECTION_NAMESPACE must be deterministic; caller override / tamper is refuse-closed."""

    namespace = _write_synthetic_first_recovery(tmp_path)
    manifest_path = tmp_path / "correction-manifest.json"
    created = create_correction_recovery_manifest(
        destination=manifest_path,
        first_recovery_namespace=namespace,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    receipt_path = tmp_path / "correction-authorization.json"
    receipt = create_correction_authorization_receipt(
        manifest_path=manifest_path,
        destination=receipt_path,
        expected_code_commit=CORRECTION_COMMIT,
    )
    expected_namespace = f"{CORRECTION_RAW_STORAGE_NAMESPACE}{created.correction_id}"
    assert receipt.correction_namespace == expected_namespace

    # Change only CORRECTION_NAMESPACE in the persisted receipt.
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    persisted["CORRECTION_NAMESPACE"] = "data/phase35/historical/corrections/attacker-chosen"
    assert persisted["CORRECTION_NAMESPACE"] != expected_namespace
    receipt_path.write_text(
        json.dumps(persisted, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(CorrectionAuthorizationError, match="namespace_mismatch") as caught:
        load_authorized_correction_manifest(
            manifest_path,
            authorization_path=receipt_path,
            expected_code_commit=CORRECTION_COMMIT,
        )

    assert caught.value.code == "namespace_mismatch"
    create_params = inspect.signature(create_correction_authorization_receipt).parameters
    assert "correction_namespace" not in create_params
    # Fail-closed: never returns an AuthorizedCorrectionManifest / network_authorized=True.


def test_first_recovery_artifacts_remain_immutable_through_manifest_and_overlay(
    tmp_path: Path,
) -> None:
    namespace = _write_synthetic_first_recovery(tmp_path)
    before = _fingerprint(namespace)
    manifest_path = tmp_path / "correction-manifest.json"
    created = create_correction_recovery_manifest(
        destination=manifest_path,
        first_recovery_namespace=namespace,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    overlay_root = tmp_path / "overlays"
    correction_id = created.correction_id
    assert correction_id is not None
    correction_ns = overlay_root / correction_id
    _seed_fake_correction_overlay(
        correction_ns,
        corrected_identity=created.identities[0],
        family_id="fam-a",
        token="tok-family",
        start_ts=1_748_000_000,
        end_ts=1_748_100_000,
    )
    view_root = tmp_path / "corrected-view"
    CorrectionOverlayService(
        first_recovery_namespace=namespace,
        correction_namespace=correction_ns,
    ).materialize_corrected_audit_view(destination=view_root)
    after = _fingerprint(namespace)
    assert after == before
    assert view_root.resolve() != namespace.resolve()


def test_overlay_precedence_feeds_corrected_v2_audit_view(tmp_path: Path) -> None:
    namespace = _write_synthetic_first_recovery(tmp_path)
    start_ts = 1_748_000_000
    end_ts = 1_748_100_000
    correct_identity = canonical_clob_identity(
        market="tok-family", start_ts=start_ts, end_ts=end_ts, fidelity=60
    )
    overlay_ns = tmp_path / "correction-overlay"
    _seed_fake_correction_overlay(
        overlay_ns,
        corrected_identity=correct_identity,
        family_id="fam-a",
        token="tok-family",
        start_ts=start_ts,
        end_ts=end_ts,
    )
    view_root = tmp_path / "corrected-view"
    CorrectionOverlayService(
        first_recovery_namespace=namespace,
        correction_namespace=overlay_ns,
    ).materialize_corrected_audit_view(destination=view_root)

    # Wrong evidence remains available under the first-recovery corpus path only.
    first_parsed = json.loads((namespace / "parsed" / "clob.json").read_text(encoding="utf-8"))
    assert any(row["identity"] != correct_identity for row in first_parsed)

    audit = offline_v2_corpus_audit(view_root)
    assert audit["UNRESOLVED_CORRECTION_CLOB_IDENTITY_COUNT"] == 0
    assert audit["CORRECTION_GAMMA_IDENTITY_COUNT"] == 0
    assert audit["CORRECTION_ECMWF_IDENTITY_COUNT"] == 0
    view_map = json.loads((view_root / "plans" / "clob_cell_map.json").read_text(encoding="utf-8"))
    assert correct_identity in view_map
    assert view_map[correct_identity][0]["event_family_id"] == "fam-a"


def test_execution_refused_without_receipt_and_no_network(tmp_path: Path) -> None:
    namespace = _write_synthetic_first_recovery(tmp_path)
    manifest_path = tmp_path / "correction-manifest.json"
    create_correction_recovery_manifest(
        destination=manifest_path,
        first_recovery_namespace=namespace,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    http = MagicMock()
    service = CorrectionRecoveryService(
        manifest_path=manifest_path,
        authorization_path=tmp_path / "absent-correction-authorization.json",
        correction_root=tmp_path / "corrections",
        first_recovery_namespace=namespace,
        http=http,
        expected_code_commit=CORRECTION_COMMIT,
    )
    with pytest.raises(CorrectionAuthorizationError, match="missing_authorization"):
        service.run()
    http.get.assert_not_called()
    http.request.assert_not_called()
    params = inspect.signature(CorrectionRecoveryService.__init__).parameters
    for forbidden in ("network_authorized", "enable_network", "force_network", "allow_network"):
        assert forbidden not in params


def test_plan_audit_overlay_paths_have_no_network_methods(tmp_path: Path) -> None:
    namespace = _write_synthetic_first_recovery(tmp_path)
    http = MagicMock()
    derive_v2_correction_targets(namespace)
    create_correction_recovery_manifest(
        destination=tmp_path / "m.json",
        first_recovery_namespace=namespace,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    overlay_ns = tmp_path / "overlay"
    start_ts = 1_748_000_000
    end_ts = 1_748_100_000
    identity = canonical_clob_identity(
        market="tok-family", start_ts=start_ts, end_ts=end_ts, fidelity=60
    )
    _seed_fake_correction_overlay(
        overlay_ns,
        corrected_identity=identity,
        family_id="fam-a",
        token="tok-family",
        start_ts=start_ts,
        end_ts=end_ts,
    )
    CorrectionOverlayService(
        first_recovery_namespace=namespace,
        correction_namespace=overlay_ns,
    ).materialize_corrected_audit_view(destination=tmp_path / "view")
    offline_v2_corpus_audit(tmp_path / "view")
    http.get.assert_not_called()
    http.request.assert_not_called()


def _seed_fake_correction_overlay(
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
    (namespace / "plans" / "clob_cell_map.json").write_text(
        json.dumps(cell_map, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
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
    (namespace / "parsed" / "clob.json").write_text(
        json.dumps(parsed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
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


def test_real_five_identity_manifest_shape_offline(tmp_path: Path) -> None:
    """Manifest from real first recovery is offline-only and exactly-five bound."""

    before = _fingerprint(REAL_FIRST_RECOVERY)
    created = create_correction_recovery_manifest(
        destination=tmp_path / "real-correction-manifest.json",
        first_recovery_namespace=REAL_FIRST_RECOVERY,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 15, 0, tzinfo=UTC),
    )
    assert created.payload["CLOB_CORRECTION_IDENTITIES"] == V2_CORRECTION_TARGET_COUNT
    assert created.payload["GAMMA_CORRECTION_IDENTITIES"] == 0
    assert created.payload["ECMWF_CORRECTION_IDENTITIES"] == 0
    assert len(created.payload["CORRECTION_ENTRIES"]) == V2_CORRECTION_PROVENANCE_COUNT
    assert created.payload["FIRST_RECOVERY_COLLECTION_ID"] == FIRST_RECOVERY_COLLECTION_ID
    assert created.payload["V2_CORRECTION_AUDIT_SOURCE"] == FIRST_RECOVERY_COLLECTION_ID
    assert set(created.identities) == set(
        row["corrected_identity"] for row in created.payload["CORRECTION_ENTRIES"]
    )
    after = _fingerprint(REAL_FIRST_RECOVERY)
    assert after == before


def _authorize_five_identity_correction(tmp_path: Path) -> tuple[Path, Path, str, tuple[str, ...]]:
    manifest_path = tmp_path / "correction-manifest.json"
    created = create_correction_recovery_manifest(
        destination=manifest_path,
        first_recovery_namespace=REAL_FIRST_RECOVERY,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 15, 0, tzinfo=UTC),
    )
    assert created.correction_id is not None
    receipt_path = tmp_path / "correction-authorization.json"
    create_correction_authorization_receipt(
        manifest_path=manifest_path,
        destination=receipt_path,
        expected_code_commit=CORRECTION_COMMIT,
        authorized_at=_clock(),
    )
    return manifest_path, receipt_path, created.correction_id, created.identities


def test_authorized_five_identity_execution_isolates_planned_gets_and_parent(
    tmp_path: Path,
) -> None:
    """Fake transport only: exactly five manifest GETs; first recovery + real artifacts untouched."""

    assert REAL_FIRST_RECOVERY.is_dir()
    assert REAL_CORRECTION.is_dir()
    before_first = _fingerprint(REAL_FIRST_RECOVERY)
    before_correction = _fingerprint(REAL_CORRECTION)

    manifest_path, receipt_path, correction_id, identities = _authorize_five_identity_correction(
        tmp_path
    )
    assert len(identities) == V2_CORRECTION_TARGET_COUNT

    transport = RecordingGetTransport({"/prices-history": _clob_history_payload()})
    service = CorrectionRecoveryService(
        manifest_path=manifest_path,
        authorization_path=receipt_path,
        correction_root=tmp_path / "corrections",
        first_recovery_namespace=REAL_FIRST_RECOVERY,
        http=_http(transport),
        expected_code_commit=CORRECTION_COMMIT,
    )
    result = service.run()

    assert result.correction_id == correction_id
    assert result.planned_gets == V2_CORRECTION_TARGET_COUNT
    assert result.recovered_gets == V2_CORRECTION_TARGET_COUNT
    assert result.failed_gets == 0
    assert result.new_gets == V2_CORRECTION_TARGET_COUNT
    assert result.stage is CollectionStage.COMPLETE
    assert result.terminal is True

    clob_calls = [call for call in transport.calls if "/prices-history" in call[1]]
    assert len(clob_calls) == V2_CORRECTION_TARGET_COUNT
    assert all(method == "GET" for method, _url in transport.calls)
    assert not any("public-search" in url for _method, url in transport.calls)
    assert not any("open-meteo" in url for _method, url in transport.calls)

    namespace = tmp_path / "corrections" / correction_id
    assert namespace.is_dir()
    parsed = json.loads((namespace / "parsed" / "clob.json").read_text(encoding="utf-8"))
    assert {row["identity"] for row in parsed} == set(identities)
    assert (namespace / "plans" / "clob_cell_map.json").is_file()
    assert (namespace / "ledger.jsonl").is_file()
    progress = json.loads((namespace / "progress.json").read_text(encoding="utf-8"))
    assert progress["terminal"] is True
    assert progress["stage"] == CollectionStage.COMPLETE.value
    lineage = json.loads((namespace / "correction_lineage.json").read_text(encoding="utf-8"))
    assert lineage["CORRECTION_ID"] == correction_id
    assert lineage["V2_CORRECTION_AUDIT_SOURCE"] == FIRST_RECOVERY_COLLECTION_ID
    assert lineage["CORRECTION_NAMESPACE"] == f"{CORRECTION_RAW_STORAGE_NAMESPACE}{correction_id}"

    assert _fingerprint(REAL_FIRST_RECOVERY) == before_first
    assert _fingerprint(REAL_CORRECTION) == before_correction
    assert REAL_CORRECTION.resolve() != namespace.resolve()


def test_execution_refuses_non_five_correction_manifest(tmp_path: Path) -> None:
    namespace = _write_synthetic_first_recovery(tmp_path)
    manifest_path = tmp_path / "correction-manifest.json"
    created = create_correction_recovery_manifest(
        destination=manifest_path,
        first_recovery_namespace=namespace,
        code_commit=CORRECTION_COMMIT,
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    assert created.payload["CLOB_CORRECTION_IDENTITIES"] == 1
    receipt_path = tmp_path / "correction-authorization.json"
    create_correction_authorization_receipt(
        manifest_path=manifest_path,
        destination=receipt_path,
        expected_code_commit=CORRECTION_COMMIT,
    )
    transport = RecordingGetTransport({"/prices-history": _clob_history_payload()})
    service = CorrectionRecoveryService(
        manifest_path=manifest_path,
        authorization_path=receipt_path,
        correction_root=tmp_path / "corrections",
        first_recovery_namespace=namespace,
        http=_http(transport),
        expected_code_commit=CORRECTION_COMMIT,
    )
    with pytest.raises(CorrectionAuthorizationError, match="target_count_mismatch") as caught:
        service.run()
    assert caught.value.code == "target_count_mismatch"
    assert transport.calls == []


def test_execution_refuses_preexisting_correction_execution_artifacts(tmp_path: Path) -> None:
    manifest_path, receipt_path, correction_id, _identities = _authorize_five_identity_correction(
        tmp_path
    )
    namespace = tmp_path / "corrections" / correction_id
    namespace.mkdir(parents=True)
    (namespace / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    transport = RecordingGetTransport({"/prices-history": _clob_history_payload()})
    service = CorrectionRecoveryService(
        manifest_path=manifest_path,
        authorization_path=receipt_path,
        correction_root=tmp_path / "corrections",
        first_recovery_namespace=REAL_FIRST_RECOVERY,
        http=_http(transport),
        expected_code_commit=CORRECTION_COMMIT,
    )
    with pytest.raises(CorrectionAuthorizationError, match="preexisting_execution") as caught:
        service.run()
    assert caught.value.code == "preexisting_execution"
    assert transport.calls == []
