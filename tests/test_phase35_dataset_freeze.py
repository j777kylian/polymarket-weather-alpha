"""Production dataset-freeze tests. Fake GET transports and temp roots only.

Does not create production manifests/receipts, contact providers, or write git.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from tests.fakes import RecordingGetTransport
from tests.test_phase35_full_collection_orchestrator import (
    _production_routes,
    _service,
)
from weather_alpha.cli import main
from weather_alpha.phase35.full_collection.audit import (
    audit_dataset,
    build_dataset_audit_reports,
)
from weather_alpha.phase35.full_collection.corpus import FullCollectionCorpusAssembler
from weather_alpha.phase35.full_collection.freeze import (
    DatasetFreezeStatus,
    build_production_dataset_freeze,
)
from weather_alpha.phase35.full_collection.orchestrator import CollectionStage
from weather_alpha.phase35.full_collection.policy import CHECKPOINTS, END_DATE, START_DATE
from weather_alpha.phase35.full_collection.provenance import assert_text_has_no_machine_roots
from weather_alpha.research.reports import write_report_pair

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDERS = frozenset({"", "none", "uncollected", "0" * 64})


def _namespace(tmp_path: Path, collection_id: str) -> Path:
    return tmp_path / "collections" / collection_id


def _complete_collection(tmp_path: Path) -> tuple[str, RecordingGetTransport, Path]:
    transport = RecordingGetTransport(_production_routes())
    result = _service(tmp_path, transport).run()
    assert result.stage is CollectionStage.COMPLETE
    return result.collection_id, transport, tmp_path / "collections"


def _persist_historical_audit(collection_root: Path, collection_id: str) -> None:
    corpus = FullCollectionCorpusAssembler(
        collection_root=collection_root,
        collection_id=collection_id,
    ).assemble()
    audit = audit_dataset(expected=corpus.expected, observations=corpus.observations)
    reports_dir = collection_root / collection_id / "reports"
    machine, human = build_dataset_audit_reports(audit, collection_not_executed=False)
    write_report_pair(
        reports_dir / "phase35_historical_audit.md",
        reports_dir / "phase35_historical_audit.json",
        human,
        machine,
    )


def _ready_freeze_inputs(tmp_path: Path) -> tuple[str, RecordingGetTransport, Path]:
    collection_id, transport, collection_root = _complete_collection(tmp_path)
    _persist_historical_audit(collection_root, collection_id)
    return collection_id, transport, collection_root


def _freeze_path(collection_root: Path, collection_id: str) -> Path:
    return collection_root / collection_id / "reports" / "phase35_dataset_freeze.json"


def _assert_real_sha256(value: str | None) -> None:
    assert value is not None
    assert value not in _PLACEHOLDERS
    assert _SHA256_RE.fullmatch(value) is not None


def _load_freeze_payload(collection_root: Path, collection_id: str) -> dict[str, Any]:
    path = _freeze_path(collection_root, collection_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _mutate_json_list(path: Path, mutator: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    mutator(payload)
    _write_json(path, payload)


def test_ready_corpus_emits_freeze_with_real_hashes_and_counts(tmp_path: Path) -> None:
    collection_id, transport, collection_root = _ready_freeze_inputs(tmp_path)
    before_calls = list(transport.calls)
    result = build_production_dataset_freeze(
        collection_root=collection_root,
        collection_id=collection_id,
    )
    assert result.status is DatasetFreezeStatus.SUCCESS
    assert result.dataset_freeze_created is True
    assert result.dataset_id == f"phase35-dataset-{collection_id}"
    freeze_file = _freeze_path(collection_root, collection_id)
    assert freeze_file.is_file()
    payload = json.loads(freeze_file.read_text(encoding="utf-8"))
    _assert_real_sha256(payload["MANIFEST_SHA256"])
    _assert_real_sha256(payload["RAW_INDEX_SHA256"])
    _assert_real_sha256(payload["CANONICAL_DATASET_SHA256"])
    _assert_real_sha256(payload["REPORT_SHA256"])
    _assert_real_sha256(result.manifest_sha256)
    _assert_real_sha256(result.raw_index_sha256)
    _assert_real_sha256(result.canonical_dataset_sha256)
    _assert_real_sha256(result.audit_report_sha256)
    _assert_real_sha256(result.freeze_sha256)
    assert payload["MANIFEST_SHA256"] == result.manifest_sha256
    assert payload["REPORT_SHA256"] == result.audit_report_sha256
    assert payload["EVENT_COUNT"] == 1
    assert payload["SNAPSHOT_COUNT"] == 6
    assert payload["DATE_RANGE"] == {"end": END_DATE, "start": START_DATE}
    assert payload["CHECKPOINT_COUNTS"] == {str(lead): 1 for lead in CHECKPOINTS}
    assert payload["CITY_COUNTS"]["paris"] == 6
    assert payload["STATION_COUNTS"]
    assert payload["MONTH_COUNTS"]
    assert payload["MISSINGNESS_SUMMARY"]
    assert payload["QUARANTINE_SUMMARY"]["count"] == 0
    assert transport.calls == before_calls


def test_dataset_not_ready_is_refused_without_freeze_artifact(tmp_path: Path) -> None:
    collection_id, _transport, collection_root = _complete_collection(tmp_path)
    observations_path = _namespace(tmp_path, collection_id) / "observations.json"

    def mark_unusable(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            row["usable"] = False
            row["observed"] = False
            row["missing_reasons"] = ["missing"]

    _mutate_json_list(observations_path, mark_unusable)
    _persist_historical_audit(collection_root, collection_id)
    result = build_production_dataset_freeze(
        collection_root=collection_root,
        collection_id=collection_id,
    )
    assert result.status is DatasetFreezeStatus.REFUSED
    assert result.dataset_freeze_created is False
    assert result.as_dict()["DATASET_FREEZE_CREATED"] == "NO"
    assert not _freeze_path(collection_root, collection_id).exists()


def test_interrupted_or_not_complete_is_refused(tmp_path: Path) -> None:
    collection_id, _transport, collection_root = _ready_freeze_inputs(tmp_path)
    progress_path = _namespace(tmp_path, collection_id) / "progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["stage"] = CollectionStage.INTERRUPTED_RESUMABLE.value
    progress["terminal"] = True
    _write_json(progress_path, progress)
    result = build_production_dataset_freeze(
        collection_root=collection_root,
        collection_id=collection_id,
    )
    assert result.status is DatasetFreezeStatus.REFUSED
    assert result.reason == "not_complete"
    assert not _freeze_path(collection_root, collection_id).exists()


def test_freeze_is_deterministic_across_repeated_offline_runs(tmp_path: Path) -> None:
    collection_id, _transport, collection_root = _ready_freeze_inputs(tmp_path)
    first = build_production_dataset_freeze(
        collection_root=collection_root,
        collection_id=collection_id,
    )
    first_payload = _load_freeze_payload(collection_root, collection_id)
    second = build_production_dataset_freeze(
        collection_root=collection_root,
        collection_id=collection_id,
    )
    second_payload = _load_freeze_payload(collection_root, collection_id)
    assert first.status is DatasetFreezeStatus.SUCCESS
    assert second.status is DatasetFreezeStatus.SUCCESS
    assert first_payload == second_payload
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.raw_index_sha256 == second.raw_index_sha256
    assert first.canonical_dataset_sha256 == second.canonical_dataset_sha256
    assert first.audit_report_sha256 == second.audit_report_sha256
    assert first.freeze_sha256 == second.freeze_sha256


def test_raw_tamper_refuses_freeze(tmp_path: Path) -> None:
    collection_id, _transport, collection_root = _ready_freeze_inputs(tmp_path)
    namespace = _namespace(tmp_path, collection_id)
    raw_files = list((namespace / "historical" / "raw").rglob("*.json"))
    assert raw_files
    raw_files[0].write_text('{"tampered": true}\n', encoding="utf-8")
    result = build_production_dataset_freeze(
        collection_root=collection_root,
        collection_id=collection_id,
    )
    assert result.status is DatasetFreezeStatus.REFUSED
    assert result.reason == "raw_hash_mismatch"
    assert not _freeze_path(collection_root, collection_id).exists()


def test_canonical_corpus_tamper_after_audit_is_refused(tmp_path: Path) -> None:
    collection_id, _transport, collection_root = _ready_freeze_inputs(tmp_path)
    namespace = _namespace(tmp_path, collection_id)

    def relabel_city(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            row["city"] = "london"

    _mutate_json_list(namespace / "expected_cells.json", relabel_city)
    _mutate_json_list(namespace / "observations.json", relabel_city)
    result = build_production_dataset_freeze(
        collection_root=collection_root,
        collection_id=collection_id,
    )
    assert result.status is DatasetFreezeStatus.REFUSED
    assert result.reason == "audit_mismatch"
    assert not _freeze_path(collection_root, collection_id).exists()


def test_audit_tamper_is_refused(tmp_path: Path) -> None:
    collection_id, _transport, collection_root = _ready_freeze_inputs(tmp_path)
    audit_path = _namespace(tmp_path, collection_id) / "reports" / "phase35_historical_audit.json"
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    payload["measured_data"]["PHASE35_DATASET_READY"] = False
    payload["inferences"]["PHASE35_DATASET_READY"] = False
    _write_json(audit_path, payload)
    result = build_production_dataset_freeze(
        collection_root=collection_root,
        collection_id=collection_id,
    )
    assert result.status is DatasetFreezeStatus.REFUSED
    assert result.reason == "audit_mismatch"
    assert not _freeze_path(collection_root, collection_id).exists()


def test_real_counts_are_derived_from_corpus_not_placeholders(tmp_path: Path) -> None:
    collection_id, _transport, collection_root = _ready_freeze_inputs(tmp_path)
    result = build_production_dataset_freeze(
        collection_root=collection_root,
        collection_id=collection_id,
    )
    assert result.status is DatasetFreezeStatus.SUCCESS
    payload = _load_freeze_payload(collection_root, collection_id)
    assert payload["EVENT_COUNT"] > 0
    assert payload["SNAPSHOT_COUNT"] > 0
    assert sum(payload["CHECKPOINT_COUNTS"].values()) == payload["SNAPSHOT_COUNT"]
    assert sum(payload["CITY_COUNTS"].values()) == payload["SNAPSHOT_COUNT"]
    assert sum(payload["STATION_COUNTS"].values()) == payload["SNAPSHOT_COUNT"]
    assert sum(payload["MONTH_COUNTS"].values()) == payload["SNAPSHOT_COUNT"]
    assert payload["MISSINGNESS_SUMMARY"]["expected_count"] == payload["SNAPSHOT_COUNT"]
    assert payload["QUARANTINE_SUMMARY"]["count"] >= 0


def test_freeze_artifact_has_no_absolute_path_leakage(tmp_path: Path) -> None:
    collection_id, _transport, collection_root = _ready_freeze_inputs(tmp_path)
    result = build_production_dataset_freeze(
        collection_root=collection_root,
        collection_id=collection_id,
    )
    assert result.status is DatasetFreezeStatus.SUCCESS
    text = _freeze_path(collection_root, collection_id).read_text(encoding="utf-8")
    assert_text_has_no_machine_roots(text)
    encoded = json.dumps(result.as_dict(), sort_keys=True, ensure_ascii=True)
    assert_text_has_no_machine_roots(encoded)
    for leak in ("/tmp/", "/Users/", "/home/"):
        assert leak not in text
        assert leak not in encoded


def test_freeze_makes_zero_provider_calls(tmp_path: Path) -> None:
    collection_id, transport, collection_root = _ready_freeze_inputs(tmp_path)
    before = list(transport.calls)
    assert before
    result = build_production_dataset_freeze(
        collection_root=collection_root,
        collection_id=collection_id,
    )
    assert result.status is DatasetFreezeStatus.SUCCESS
    assert transport.calls == before


def test_cli_freeze_dataset_reports_id_and_hash(tmp_path: Path) -> None:
    collection_id, _transport, collection_root = _ready_freeze_inputs(tmp_path)
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
    assert invoked.exit_code == 0, invoked.output
    assert "DATASET_FREEZE_CREATED" in invoked.output
    assert "YES" in invoked.output
    assert f"phase35-dataset-{collection_id}" in invoked.output
    assert _freeze_path(collection_root, collection_id).is_file()
    for leak in ("/tmp/", "/Users/", "/home/"):
        assert leak not in invoked.output


def test_cli_freeze_dataset_refuses_not_complete(tmp_path: Path) -> None:
    collection_id, _transport, collection_root = _ready_freeze_inputs(tmp_path)
    progress_path = _namespace(tmp_path, collection_id) / "progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["stage"] = CollectionStage.CLOB_COLLECTION.value
    progress["terminal"] = False
    _write_json(progress_path, progress)
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
    assert invoked.exit_code == 2
    assert "DATASET_FREEZE_CREATED" in invoked.output
    assert "NO" in invoked.output
    assert not _freeze_path(collection_root, collection_id).exists()
