"""Offline full-collection orchestrator and corpus->audit bridge tests.

Fake/in-memory GET transports and temporary roots only. No provider network,
credentials, production manifests, production receipts, or git writes.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests.fakes import RecordingGetTransport
from weather_alpha.cli import main
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyResponse
from weather_alpha.phase35.full_collection.audit import (
    audit_dataset,
    build_dataset_audit_reports,
)
from weather_alpha.phase35.full_collection.budget import StaticDiskProbe
from weather_alpha.phase35.full_collection.corpus import FullCollectionCorpusAssembler
from weather_alpha.phase35.full_collection.freeze import (
    DatasetFreezeStatus,
    build_production_dataset_freeze,
)
from weather_alpha.phase35.full_collection.ledger import ResultClassification
from weather_alpha.phase35.full_collection.manifest import (
    ManifestAuthorizationError,
    create_authorization_receipt,
    create_immutable_manifest,
    load_authorized_manifest,
)
from weather_alpha.phase35.full_collection.orchestrator import (
    CollectionStage,
    FullHistoricalCollectionService,
)
from weather_alpha.phase35.full_collection.policy import (
    AUTHORIZATION_SCHEMA_VERSION,
    CHECKPOINTS,
    CLOB_ENDPOINT,
    ECMWF_ENDPOINT,
    END_DATE,
    FORECAST_MODEL,
    GAMMA_ENDPOINT,
    PARSER_SCHEMA_VERSION,
    REQUEST_POLICY_VERSION,
    START_DATE,
    STORAGE_PREFLIGHT_MIN_BYTES,
    TARGET_CITIES_CANONICAL,
    YES_PENDING_FINAL_REVIEW,
)
from weather_alpha.phase35.full_collection.schedule import gamma_identities
from weather_alpha.research.reports import write_report_pair

COMMIT = "orchestrator-test-commit"
PARIS_DAY = "2026-03-01"
FAMILY_EVENT_ID = "evt-paris-2026-03-01"


def _clock() -> datetime:
    return datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _write_authorized_manifest(tmp_path: Path) -> Path:
    dest = tmp_path / "manifests" / "authorized.json"
    result = create_immutable_manifest(
        destination=dest,
        code_commit=COMMIT,
        created_at=_clock(),
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES),
    )
    assert result.authorized is True
    assert result.written is True
    return dest


def _write_authorization_receipt(manifest: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "authorization.json"
    create_authorization_receipt(
        manifest_path=manifest,
        destination=dest,
        expected_code_commit=COMMIT,
        authorized_at=_clock(),
    )
    return dest


def _mutate_json(path: Path, **updates: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(updates)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _complete_paris_family() -> dict[str, Any]:
    buckets = (
        ("10°C or below", "yes-below", "no-below", ["0", "1"]),
        ("11°C", "yes-11", "no-11", ["1", "0"]),
        ("12°C or higher", "yes-above", "no-above", ["0", "1"]),
    )
    markets: list[dict[str, Any]] = []
    for title, yes_tok, no_tok, prices in buckets:
        slug_bucket = title.lower().replace("°c", "c").replace(" ", "-")
        markets.append(
            {
                "id": f"m-{yes_tok}",
                "question": "Highest temperature in Paris on March 1, 2026?",
                "conditionId": f"0x{yes_tok}",
                "slug": f"highest-temperature-in-paris-on-march-1-2026-{slug_bucket}",
                "description": (
                    "Station at Paris Charles de Gaulle Airport LFPG. "
                    "https://www.wunderground.com/history/daily/fr/paris/LFPG."
                ),
                "groupItemTitle": title,
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": f'["{yes_tok}", "{no_tok}"]',
                "outcomePrices": json.dumps(prices),
                "closed": True,
                "resolved": True,
                "active": False,
                "eventDate": PARIS_DAY,
                "events": [{"id": FAMILY_EVENT_ID}],
            }
        )
    return {"events": [{"id": FAMILY_EVENT_ID, "markets": markets}], "markets": []}


def _gapped_paris_family() -> dict[str, Any]:
    payload = _complete_paris_family()
    events = payload["events"]
    assert isinstance(events, list)
    parent = events[0]
    assert isinstance(parent, dict)
    markets = parent["markets"]
    assert isinstance(markets, list)
    parent["markets"] = [markets[0], markets[2]]
    return payload


def _ecmwf_paris_payload() -> dict[str, Any]:
    times = [f"2026-03-01T{hour:02d}:00" for hour in range(24)]
    temps = [10.0 + (hour * 0.25) for hour in range(24)]
    return {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 3600,
        "hourly_units": {"temperature_2m": "°C"},
        "hourly": {"time": times, "temperature_2m": temps},
    }


def _pre_decision_history() -> dict[str, Any]:
    return {
        "history": [
            {"t": int(datetime(2026, 2, 26, 12, tzinfo=UTC).timestamp()), "p": 0.41},
        ]
    }


def _post_decision_history() -> dict[str, Any]:
    return {
        "history": [
            {"t": int(datetime(2026, 3, 2, 12, tzinfo=UTC).timestamp()), "p": 0.77},
        ]
    }


def _gamma_router(payload: dict[str, Any]) -> Any:
    def handler(params: Mapping[str, Any]) -> dict[str, Any]:
        query = str(params.get("q") or "").lower()
        if "paris" in query and "march 1" in query and "2026" in query:
            return payload
        return {"events": [], "markets": []}

    return handler


def _production_routes(
    *,
    gamma: dict[str, Any] | None = None,
    ecmwf: dict[str, Any] | None = None,
    clob: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "/public-search": _gamma_router(gamma if gamma is not None else _complete_paris_family()),
        "single-runs-api.open-meteo.com": ecmwf if ecmwf is not None else _ecmwf_paris_payload(),
        "/prices-history": clob if clob is not None else _pre_decision_history(),
    }


def _http(transport: Any) -> ReadOnlyHttpClient:
    return ReadOnlyHttpClient(
        transport=transport,
        max_retries=0,
        retry_statuses=frozenset(),
        sleeper=lambda _seconds: None,
    )


def _service(
    tmp_path: Path,
    transport: RecordingGetTransport,
    *,
    manifest: Path | None = None,
    authorization_path: Path | None = None,
    execution_pairs: tuple[tuple[str, str], ...] | None = (("paris", PARIS_DAY),),
    **kwargs: Any,
) -> FullHistoricalCollectionService:
    manifest_path = manifest or _write_authorized_manifest(tmp_path / "auth")
    receipt_path = authorization_path or _write_authorization_receipt(
        manifest_path, tmp_path / "auth"
    )
    return FullHistoricalCollectionService(
        manifest_path=manifest_path,
        authorization_path=receipt_path,
        collection_root=tmp_path / "collections",
        http=_http(transport),
        expected_code_commit=COMMIT,
        execution_pairs=execution_pairs,
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
        sleeper=lambda _seconds: None,
        clock=_clock,
        **kwargs,
    )


def test_gamma_schedule_is_frozen_540_city_dates() -> None:
    identities = gamma_identities()
    assert len(identities) == 540
    assert identities[0] == f"gamma:{TARGET_CITIES_CANONICAL[0]}:{START_DATE}"
    assert identities[-1] == f"gamma:{TARGET_CITIES_CANONICAL[-1]}:{END_DATE}"


def test_load_authorized_manifest_refuses_missing_invalid_unauthorized_and_mismatches(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "authorization.json"
    missing = tmp_path / "absent.json"
    with pytest.raises(ManifestAuthorizationError, match="missing"):
        load_authorized_manifest(missing, authorization_path=receipt, expected_code_commit=COMMIT)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ManifestAuthorizationError, match="invalid"):
        load_authorized_manifest(invalid, authorization_path=receipt, expected_code_commit=COMMIT)

    unauthorized = tmp_path / "unauthorized.json"
    unauthorized.write_text(
        json.dumps({"authorized": False, "status": "REQUEST_BUDGET_REDESIGN_REQUIRED"}),
        encoding="utf-8",
    )
    with pytest.raises(ManifestAuthorizationError, match="not_authorized"):
        load_authorized_manifest(
            unauthorized, authorization_path=receipt, expected_code_commit=COMMIT
        )

    authorized = _write_authorized_manifest(tmp_path)
    with pytest.raises(ManifestAuthorizationError, match="code_mismatch"):
        load_authorized_manifest(
            authorized, authorization_path=receipt, expected_code_commit="other-commit"
        )

    payload = json.loads(authorized.read_text(encoding="utf-8"))
    payload["REQUEST_POLICY"]["version"] = "phase35-full-collection-request-policy-v1"
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestAuthorizationError, match="policy_mismatch"):
        load_authorized_manifest(policy, authorization_path=receipt, expected_code_commit=COMMIT)

    payload = json.loads(authorized.read_text(encoding="utf-8"))
    payload["END_DATE"] = "2026-05-28"
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestAuthorizationError, match="scope_mismatch"):
        load_authorized_manifest(scope, authorization_path=receipt, expected_code_commit=COMMIT)


def test_service_constructor_rejects_network_authorized_boolean(tmp_path: Path) -> None:
    manifest = _write_authorized_manifest(tmp_path)
    receipt = _write_authorization_receipt(manifest, tmp_path)
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(TypeError):
        FullHistoricalCollectionService(
            manifest_path=manifest,
            authorization_path=receipt,
            collection_root=tmp_path / "collections",
            http=_http(transport),
            expected_code_commit=COMMIT,
            network_authorized=True,  # type: ignore[call-arg]  # intentionally rejected at runtime
        )
    assert (
        "network_authorized"
        not in inspect.signature(FullHistoricalCollectionService.__init__).parameters
    )
    assert transport.calls == []


def test_valid_manifest_and_matching_receipt_authorizes_fake_transport(tmp_path: Path) -> None:
    transport = RecordingGetTransport(_production_routes())
    service = _service(tmp_path, transport)
    loaded = load_authorized_manifest(
        service.manifest_path,
        authorization_path=service.authorization_path,
        expected_code_commit=COMMIT,
    )
    assert loaded.network_authorized is True
    before_receipt = service.authorization_path.read_bytes()
    before_manifest = service.manifest_path.read_bytes()
    result = service.run()
    assert result.stage is CollectionStage.COMPLETE
    assert transport.calls
    assert service.authorization_path.read_bytes() == before_receipt
    assert service.manifest_path.read_bytes() == before_manifest


def test_tampered_manifest_after_authorize_is_refused(tmp_path: Path) -> None:
    manifest = _write_authorized_manifest(tmp_path)
    receipt = _write_authorization_receipt(manifest, tmp_path)
    original_receipt = receipt.read_bytes()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["CREATED_AT"] = "2020-01-01T00:00:00+00:00"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(ManifestAuthorizationError, match="manifest_sha_mismatch"):
        _service(tmp_path, transport, manifest=manifest, authorization_path=receipt).run()
    assert transport.calls == []
    assert receipt.read_bytes() == original_receipt


def test_tampered_receipt_digest_binding_is_refused(tmp_path: Path) -> None:
    manifest = _write_authorized_manifest(tmp_path)
    receipt = _write_authorization_receipt(manifest, tmp_path)
    original_manifest = manifest.read_bytes()
    _mutate_json(receipt, MANIFEST_SHA256="0" * 64)
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(ManifestAuthorizationError, match="manifest_sha_mismatch"):
        _service(tmp_path, transport, manifest=manifest, authorization_path=receipt).run()
    assert transport.calls == []
    assert manifest.read_bytes() == original_manifest


def test_collection_id_mismatch_is_refused(tmp_path: Path) -> None:
    manifest = _write_authorized_manifest(tmp_path)
    receipt = _write_authorization_receipt(manifest, tmp_path)
    _mutate_json(receipt, COLLECTION_ID="phase35-hist-other")
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(ManifestAuthorizationError, match="collection_id_mismatch"):
        _service(tmp_path, transport, manifest=manifest, authorization_path=receipt).run()
    assert transport.calls == []


def test_receipt_code_commit_mismatch_is_refused(tmp_path: Path) -> None:
    manifest = _write_authorized_manifest(tmp_path)
    receipt = _write_authorization_receipt(manifest, tmp_path)
    _mutate_json(receipt, CODE_COMMIT="other-commit")
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(ManifestAuthorizationError, match="code_mismatch"):
        _service(tmp_path, transport, manifest=manifest, authorization_path=receipt).run()
    assert transport.calls == []


def test_receipt_request_policy_mismatch_is_refused(tmp_path: Path) -> None:
    manifest = _write_authorized_manifest(tmp_path)
    receipt = _write_authorization_receipt(manifest, tmp_path)
    _mutate_json(receipt, REQUEST_POLICY_VERSION="phase35-full-collection-request-policy-v1")
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(ManifestAuthorizationError, match="policy_mismatch"):
        _service(tmp_path, transport, manifest=manifest, authorization_path=receipt).run()
    assert transport.calls == []


def test_missing_authorization_receipt_is_refused(tmp_path: Path) -> None:
    manifest = _write_authorized_manifest(tmp_path)
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(ManifestAuthorizationError, match="missing_authorization"):
        FullHistoricalCollectionService(
            manifest_path=manifest,
            authorization_path=tmp_path / "no-receipt.json",
            collection_root=tmp_path / "collections",
            http=_http(transport),
            expected_code_commit=COMMIT,
            execution_pairs=(("paris", PARIS_DAY),),
            disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
            sleeper=lambda _seconds: None,
            clock=_clock,
        ).run()
    assert transport.calls == []
    assert not (tmp_path / "no-receipt.json").exists()


def test_preflight_network_authorized_flag_does_not_grant_without_receipt(
    tmp_path: Path,
) -> None:
    manifest = _write_authorized_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["PREFLIGHT"]["network_authorized"] = True
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(ManifestAuthorizationError, match="missing_authorization"):
        FullHistoricalCollectionService(
            manifest_path=manifest,
            authorization_path=tmp_path / "absent-receipt.json",
            collection_root=tmp_path / "collections",
            http=_http(transport),
            expected_code_commit=COMMIT,
            execution_pairs=(("paris", PARIS_DAY),),
            disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
            sleeper=lambda _seconds: None,
            clock=_clock,
        ).run()
    assert transport.calls == []


def test_collection_does_not_rewrite_authorization_receipt(tmp_path: Path) -> None:
    manifest = _write_authorized_manifest(tmp_path)
    receipt = _write_authorization_receipt(manifest, tmp_path)
    before = receipt.read_bytes()
    before_manifest = manifest.read_bytes()
    transport = RecordingGetTransport(_production_routes())
    result = _service(tmp_path, transport, manifest=manifest, authorization_path=receipt).run()
    assert result.stage is CollectionStage.COMPLETE
    assert receipt.read_bytes() == before
    assert manifest.read_bytes() == before_manifest


def test_invalid_authorization_schema_is_refused(tmp_path: Path) -> None:
    manifest = _write_authorized_manifest(tmp_path)
    receipt = tmp_path / "authorization.json"
    receipt.write_text(
        json.dumps({"AUTHORIZATION_SCHEMA_VERSION": "not-the-frozen-authorization-schema"}),
        encoding="utf-8",
    )
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(ManifestAuthorizationError, match="invalid_authorization"):
        _service(tmp_path, transport, manifest=manifest, authorization_path=receipt).run()
    assert transport.calls == []


def test_service_does_not_create_a_manifest(tmp_path: Path) -> None:
    missing = tmp_path / "never-created.json"
    transport = RecordingGetTransport(_production_routes())
    with pytest.raises(ManifestAuthorizationError):
        FullHistoricalCollectionService(
            manifest_path=missing,
            authorization_path=tmp_path / "never-created-authorization.json",
            collection_root=tmp_path / "collections",
            http=_http(transport),
            expected_code_commit=COMMIT,
        ).run()
    assert not missing.exists()
    assert transport.calls == []


def test_offline_production_e2e_family_to_audit_and_no_replay(tmp_path: Path) -> None:
    transport = RecordingGetTransport(_production_routes())
    service = _service(tmp_path, transport)
    first = service.run()
    assert first.stage is CollectionStage.COMPLETE
    assert first.collection_started is True
    assert first.accepted_family_count == 1
    assert first.expected_cell_count == 6
    assert first.expected_cell_count == len(CHECKPOINTS)

    namespace = tmp_path / "collections" / first.collection_id
    progress = json.loads((namespace / "progress.json").read_text(encoding="utf-8"))
    assert progress["stage"] == CollectionStage.COMPLETE.value
    expected_rows = json.loads((namespace / "expected_cells.json").read_text(encoding="utf-8"))
    assert len(expected_rows) == 6
    checkpoints = sorted(int(row["checkpoint"]) for row in expected_rows)
    assert checkpoints == sorted(CHECKPOINTS)

    corpus = FullCollectionCorpusAssembler(
        collection_root=tmp_path / "collections",
        collection_id=first.collection_id,
    ).assemble()
    assert len(corpus.expected) == 6
    assert len(corpus.observations) == 6
    audit = audit_dataset(expected=corpus.expected, observations=corpus.observations)
    assert set(audit.matrices) == {
        "DATE",
        "CITY",
        "STATION",
        "CHECKPOINT",
        "ECMWF_RUN_CYCLE",
        "EVENT_FAMILY",
        "PRICE_HISTORY",
        "MONTH",
    }
    overall_expected = sum(cell.expected_count for cell in audit.matrices["CHECKPOINT"])
    assert overall_expected == 6
    assert audit.phase35_dataset_ready is True
    reports_dir = namespace / "reports"
    machine, human = build_dataset_audit_reports(audit, collection_not_executed=False)
    write_report_pair(
        reports_dir / "phase35_historical_audit.md",
        reports_dir / "phase35_historical_audit.json",
        human,
        machine,
    )
    before_freeze_calls = list(transport.calls)
    freeze = build_production_dataset_freeze(
        collection_root=tmp_path / "collections",
        collection_id=first.collection_id,
    )
    assert freeze.status is DatasetFreezeStatus.SUCCESS
    assert freeze.dataset_freeze_created is True
    assert freeze.dataset_id == f"phase35-dataset-{first.collection_id}"
    assert freeze.freeze_sha256
    assert freeze.manifest_sha256 == progress["manifest_sha256"]
    assert freeze.raw_index_sha256
    assert freeze.canonical_dataset_sha256
    assert freeze.audit_report_sha256
    freeze_file = reports_dir / "phase35_dataset_freeze.json"
    assert freeze_file.is_file()
    freeze_payload = json.loads(freeze_file.read_text(encoding="utf-8"))
    assert freeze_payload["EVENT_COUNT"] == 1
    assert freeze_payload["SNAPSHOT_COUNT"] == 6
    assert freeze_payload["MANIFEST_SHA256"] not in {"none", "uncollected", "0" * 64}
    assert transport.calls == before_freeze_calls
    first_calls = list(transport.calls)

    restarted = FullHistoricalCollectionService(
        manifest_path=service.manifest_path,
        authorization_path=service.authorization_path,
        collection_root=tmp_path / "collections",
        http=_http(transport),
        expected_code_commit=COMMIT,
        execution_pairs=(("paris", PARIS_DAY),),
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
        sleeper=lambda _seconds: None,
        clock=_clock,
    ).run()
    assert restarted.stage is CollectionStage.COMPLETE
    assert restarted.skipped_replay is True
    assert transport.calls == first_calls
    skipped = [
        row
        for row in restarted.ledger.records()
        if row.result_classification is ResultClassification.SKIPPED_ALREADY_COMPLETE
    ]
    assert skipped


def test_gamma_ecmwf_clob_restart_skips_verified_success(tmp_path: Path) -> None:
    transport = RecordingGetTransport(_production_routes())
    first = _service(tmp_path, transport).run()
    gamma_calls = [call for call in transport.calls if "/public-search" in call[1]]
    ecmwf_calls = [call for call in transport.calls if "single-runs-api" in call[1]]
    clob_calls = [call for call in transport.calls if "/prices-history" in call[1]]
    assert gamma_calls
    assert ecmwf_calls
    assert clob_calls
    second = FullHistoricalCollectionService(
        manifest_path=tmp_path / "auth" / "manifests" / "authorized.json",
        authorization_path=tmp_path / "auth" / "authorization.json",
        collection_root=tmp_path / "collections",
        http=_http(transport),
        expected_code_commit=COMMIT,
        execution_pairs=(("paris", PARIS_DAY),),
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
        sleeper=lambda _seconds: None,
        clock=_clock,
    ).run()
    del first
    assert second.skipped_replay is True
    assert [call for call in transport.calls if "/public-search" in call[1]] == gamma_calls
    assert [call for call in transport.calls if "single-runs-api" in call[1]] == ecmwf_calls
    assert [call for call in transport.calls if "/prices-history" in call[1]] == clob_calls


def test_corrupt_sha_fail_closed(tmp_path: Path) -> None:
    transport = RecordingGetTransport(_production_routes())
    first = _service(tmp_path, transport).run()
    namespace = tmp_path / "collections" / first.collection_id
    raw_files = list((namespace / "historical" / "raw").rglob("*.json"))
    assert raw_files
    raw_files[0].write_text('{"tampered": true}\n', encoding="utf-8")
    restarted = FullHistoricalCollectionService(
        manifest_path=tmp_path / "auth" / "manifests" / "authorized.json",
        authorization_path=tmp_path / "auth" / "authorization.json",
        collection_root=tmp_path / "collections",
        http=_http(transport),
        expected_code_commit=COMMIT,
        execution_pairs=(("paris", PARIS_DAY),),
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
        sleeper=lambda _seconds: None,
        clock=_clock,
    ).run()
    assert restarted.stage is CollectionStage.FAILED_INTEGRITY
    assert restarted.collection_status == "FAILED_INTEGRITY"


def test_cli_refuses_missing_and_mismatched_manifests_without_creating(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    missing = runner.invoke(
        main,
        [
            "phase35-collect-historical",
            "--manifest",
            str(tmp_path / "nope.json"),
            "--authorization",
            str(tmp_path / "nope-authorization.json"),
            "--output-root",
            str(tmp_path / "out"),
        ],
    )
    assert missing.exit_code == 2
    assert "missing" in missing.output.lower() or "REFUSED" in missing.output
    assert "PROVIDER_REQUESTS" in missing.output
    assert not (tmp_path / "nope.json").exists()

    authorized = _write_authorized_manifest(tmp_path)
    payload = json.loads(authorized.read_text(encoding="utf-8"))
    payload["REQUEST_POLICY"]["version"] = "not-the-frozen-policy"
    bad = tmp_path / "bad-policy.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    policy = runner.invoke(
        main,
        [
            "phase35-collect-historical",
            "--manifest",
            str(bad),
            "--authorization",
            str(tmp_path / "nope-authorization.json"),
            "--output-root",
            str(tmp_path / "out"),
        ],
    )
    assert policy.exit_code == 2
    assert "policy_mismatch" in policy.output or "REFUSED" in policy.output
    assert "PROVIDER_REQUESTS" in policy.output


def test_all_three_cap_reasons_interrupt_resumable(tmp_path: Path) -> None:
    from tests.test_phase35_full_collection_contract import _mini_enforcement

    def enforcement(**overrides: Any) -> Any:
        return _mini_enforcement(**overrides)

    transport = RecordingGetTransport(_production_routes())
    provider_cap = _service(
        tmp_path / "provider",
        transport,
        enforcement=enforcement(
            gamma_identities=2,
            gamma_initial_attempts=2,
            gamma_retry_reserve=0,
            gamma_max_attempts=0,
            clob_max_attempts=10,
            ecmwf_max_attempts=10,
            global_max_attempts=10,
        ),
    ).run()
    assert provider_cap.stage is CollectionStage.INTERRUPTED_RESUMABLE
    assert provider_cap.interrupt_reason == "provider_attempt_cap_exhausted"

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

    reserve_manifest = _write_authorized_manifest(tmp_path / "reserve")
    reserve_receipt = _write_authorization_receipt(reserve_manifest, tmp_path / "reserve")
    reserve = FullHistoricalCollectionService(
        manifest_path=reserve_manifest,
        authorization_path=reserve_receipt,
        collection_root=tmp_path / "reserve-root",
        http=_http(Always429()),
        expected_code_commit=COMMIT,
        execution_pairs=(("paris", PARIS_DAY), ("london", PARIS_DAY)),
        enforcement=enforcement(
            gamma_identities=2,
            gamma_initial_attempts=2,
            gamma_retry_reserve=0,
            gamma_max_attempts=4,
            clob_max_attempts=10,
            ecmwf_max_attempts=10,
            global_max_attempts=10,
        ),
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
        sleeper=lambda _seconds: None,
        clock=_clock,
    ).run()
    assert reserve.stage is CollectionStage.INTERRUPTED_RESUMABLE
    assert reserve.interrupt_reason == "retry_reserve_exhausted"

    global_manifest = _write_authorized_manifest(tmp_path / "global")
    global_receipt = _write_authorization_receipt(global_manifest, tmp_path / "global")
    global_cap = FullHistoricalCollectionService(
        manifest_path=global_manifest,
        authorization_path=global_receipt,
        collection_root=tmp_path / "global-root",
        http=_http(RecordingGetTransport(_production_routes())),
        expected_code_commit=COMMIT,
        execution_pairs=(("paris", PARIS_DAY),),
        enforcement=enforcement(
            gamma_identities=1,
            gamma_initial_attempts=1,
            gamma_retry_reserve=1,
            gamma_max_attempts=2,
            clob_max_attempts=2,
            ecmwf_max_attempts=2,
            global_max_attempts=0,
        ),
        disk=StaticDiskProbe(free_bytes_value=STORAGE_PREFLIGHT_MIN_BYTES, used_bytes_value=0),
        sleeper=lambda _seconds: None,
        clock=_clock,
        global_attempt_cap=0,
    ).run()
    assert global_cap.stage is CollectionStage.INTERRUPTED_RESUMABLE
    assert global_cap.interrupt_reason == "global_attempt_cap_exhausted"


def test_post_decision_forecast_and_price_are_excluded(tmp_path: Path) -> None:
    future_ecmwf = _ecmwf_paris_payload()
    future_ecmwf["issued_at"] = "2026-03-15T00:00:00+00:00"
    transport = RecordingGetTransport(
        _production_routes(ecmwf=future_ecmwf, clob=_post_decision_history())
    )
    result = _service(tmp_path, transport, force_post_decision_inputs=True).run()
    corpus = FullCollectionCorpusAssembler(
        collection_root=tmp_path / "collections",
        collection_id=result.collection_id,
    ).assemble()
    reasons = {reason for row in corpus.observations for reason in row.missing_reasons}
    assert "NO_VALID_FORECAST_BEFORE_DECISION" in reasons or any(
        row.future_leakage for row in corpus.observations
    )
    assert "NO_PRE_DECISION_PRICE" in reasons or any(
        row.future_leakage for row in corpus.observations
    )
    assert all(not row.usable or not row.future_leakage for row in corpus.observations)
    audit = audit_dataset(expected=corpus.expected, observations=corpus.observations)
    assert len(corpus.expected) == 6
    assert sum(cell.expected_count for cell in audit.matrices["CHECKPOINT"]) == 6


def test_missing_forecast_stays_in_denominator(tmp_path: Path) -> None:
    empty_forecast = {
        "timezone": "Europe/Paris",
        "utc_offset_seconds": 3600,
        "hourly_units": {"temperature_2m": "°C"},
        "hourly": {"time": [], "temperature_2m": []},
    }
    transport = RecordingGetTransport(_production_routes(ecmwf=empty_forecast))
    result = _service(tmp_path, transport).run()
    corpus = FullCollectionCorpusAssembler(
        collection_root=tmp_path / "collections",
        collection_id=result.collection_id,
    ).assemble()
    assert len(corpus.expected) == 6
    audit = audit_dataset(expected=corpus.expected, observations=corpus.observations)
    assert sum(cell.expected_count for cell in audit.matrices["CHECKPOINT"]) == 6
    assert any(
        "NO_VALID_FORECAST_BEFORE_DECISION" in row.missing_reasons for row in corpus.observations
    )


def test_valid_empty_price_is_explicit(tmp_path: Path) -> None:
    transport = RecordingGetTransport(_production_routes(clob={"history": []}))
    result = _service(tmp_path, transport).run()
    corpus = FullCollectionCorpusAssembler(
        collection_root=tmp_path / "collections",
        collection_id=result.collection_id,
    ).assemble()
    assert any("PRICE_HISTORY_EMPTY" in row.missing_reasons for row in corpus.observations)
    assert all(row.has_price_history is False for row in corpus.observations)


def test_quarantine_is_auditable(tmp_path: Path) -> None:
    transport = RecordingGetTransport(_production_routes(gamma=_gapped_paris_family()))
    result = _service(tmp_path, transport).run()
    namespace = tmp_path / "collections" / result.collection_id
    quarantined = json.loads(
        (namespace / "events" / "quarantined.json").read_text(encoding="utf-8")
    )
    assert quarantined
    assert result.accepted_family_count == 0
    for leak in ("/tmp/", "/Users/", "/home/"):
        assert leak not in json.dumps(quarantined)


def test_deterministic_corpus_audit(tmp_path: Path) -> None:
    transport_a = RecordingGetTransport(_production_routes())
    first = _service(tmp_path / "a", transport_a).run()
    corpus_a = FullCollectionCorpusAssembler(
        collection_root=tmp_path / "a" / "collections",
        collection_id=first.collection_id,
    ).assemble()
    audit_a = audit_dataset(expected=corpus_a.expected, observations=corpus_a.observations)
    transport_b = RecordingGetTransport(_production_routes())
    second = _service(tmp_path / "b", transport_b).run()
    corpus_b = FullCollectionCorpusAssembler(
        collection_root=tmp_path / "b" / "collections",
        collection_id=second.collection_id,
    ).assemble()
    audit_b = audit_dataset(expected=corpus_b.expected, observations=corpus_b.observations)
    assert audit_a.as_dict()["matrices"] == audit_b.as_dict()["matrices"]
    assert audit_a.phase35_dataset_ready == audit_b.phase35_dataset_ready


def test_no_absolute_path_leak_in_canonical_artifacts(tmp_path: Path) -> None:
    transport = RecordingGetTransport(_production_routes())
    result = _service(tmp_path, transport).run()
    namespace = tmp_path / "collections" / result.collection_id
    for path in namespace.rglob("*"):
        if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
            continue
        text = path.read_text(encoding="utf-8")
        for leak in ("/tmp/", "/Users/", "/home/"):
            assert leak not in text


def test_discovery_retains_empty_ineligible_and_schema_outcomes(tmp_path: Path) -> None:
    def mixed_gamma(params: Mapping[str, Any]) -> dict[str, Any]:
        query = str(params.get("q") or "").lower()
        if "london" in query:
            return {"error": "not a search container"}
        if "milan" in query:
            return {
                "events": [
                    {
                        "id": "sports-1",
                        "markets": [
                            {
                                "id": "m-sport",
                                "question": "Will Milan win?",
                                "conditionId": "0xsport",
                                "outcomes": '["Yes", "No"]',
                                "clobTokenIds": '["yes", "no"]',
                            }
                        ],
                    }
                ],
                "markets": [],
            }
        return {"events": [], "markets": []}

    transport = RecordingGetTransport(
        {
            "/public-search": mixed_gamma,
            "single-runs-api.open-meteo.com": _ecmwf_paris_payload(),
            "/prices-history": _pre_decision_history(),
        }
    )
    result = _service(
        tmp_path,
        transport,
        execution_pairs=(
            ("london", PARIS_DAY),
            ("milan", PARIS_DAY),
            ("paris", PARIS_DAY),
        ),
    ).run()
    namespace = tmp_path / "collections" / result.collection_id
    summaries = json.loads(
        (namespace / "discovery" / "gamma_summaries.json").read_text(encoding="utf-8")
    )
    classes = {row["semantic_class"] for row in summaries}
    assert "schema_error" in classes
    assert "schema_valid_phase3_ineligible" in classes or "INELIGIBLE" in {
        row.get("classification") for row in summaries
    }
    assert "valid_empty" in classes or "VALID_EMPTY" in {
        row.get("classification") for row in summaries
    }


def test_cli_audit_historical_uses_real_corpus_assembler(tmp_path: Path) -> None:
    transport = RecordingGetTransport(_production_routes())
    result = _service(tmp_path, transport).run()
    runner = CliRunner()
    audit = runner.invoke(
        main,
        [
            "phase35-audit-historical",
            "--collection-id",
            result.collection_id,
            "--collection-root",
            str(tmp_path / "collections"),
        ],
    )
    assert audit.exit_code in {0, 2}
    assert "PHASE35_DATASET_READY" in audit.output
    assert "collection_not_executed" not in audit.output.lower() or "false" in audit.output.lower()
    for leak in ("/tmp/", "/Users/", "/home/"):
        assert leak not in audit.output


def test_frozen_science_and_request_policy_unchanged() -> None:
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
    assert FORECAST_MODEL == "ecmwf_ifs"
    assert REQUEST_POLICY_VERSION == "phase35-full-collection-request-policy-v2"
    assert AUTHORIZATION_SCHEMA_VERSION == "phase35-full-collection-authorization-v1"
    assert PARSER_SCHEMA_VERSION == "phase35-full-collection-parser-v1"
    assert GAMMA_ENDPOINT.endswith("/public-search")
    assert CLOB_ENDPOINT.endswith("/prices-history")
    assert ECMWF_ENDPOINT.endswith("/v1/forecast")
    assert YES_PENDING_FINAL_REVIEW == "YES_PENDING_FINAL_REVIEW"
